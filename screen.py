import os
import sys
import asyncio
import threading
import cv2
import customtkinter as ctk
from PIL import Image
import firebase_admin
from firebase_admin import credentials, db
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, AudioStreamTrack, RTCConfiguration, RTCIceServer, RTCIceCandidate
from av import VideoFrame, AudioFrame
import pyaudio
import numpy as np
import time

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

# ==============================================================================
# 🛠️ FIREBASE BAĞLANTISI
# ==============================================================================
def resource_path(relative_path):
    try: base_path = sys._MEIPASS
    except Exception: base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

if not firebase_admin._apps:
    try:
        cred = credentials.Certificate(resource_path("serviceAccountKey.json"))
        firebase_admin.initialize_app(cred, {
            'databaseURL': 'https://pythonmeet-bdab0-default-rtdb.europe-west1.firebasedatabase.app/'
        })
    except Exception as e:
        print("Firebase başlatılamadı:", e)
# ==============================================================================

# 🎙️ [KUSURSUZ ASENKRON SES KANALI]: Şişmeyi ve kilitlenmeyi önleyen tampon motoru
class LocalAudioTrack(AudioStreamTrack):
    def __init__(self, app):
        super().__init__()
        self.app = app

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        
        # Kararlı 48kHz için 20ms'lik ham veri boyutu = 1920 byte
        packet_size = 1920
        data = b'\x00' * packet_size
        
        if self.app.mic_enabled and len(self.app.audio_in_queue) >= packet_size:
            data = self.app.audio_in_queue[:packet_size]
            self.app.audio_in_queue = self.app.audio_in_queue[packet_size:]
        else:
            # Kuyrukta veri yoksa veya mik kapalıysa asenkron döngüyü rahatlat
            await asyncio.sleep(0.02)
            
        frame = AudioFrame.from_ndarray(np.frombuffer(data, dtype=np.int16).reshape(1, -1), format='s16', layout='mono')
        frame.sample_rate = 48000
        frame.pts = pts
        frame.time_base = time_base
        return frame

# 📹 [PÜRÜZSÜZ CANLI VİDEO MOTORU]: Gecikmeyi yok eden FPS sabitleyici
class TkinterVideoTrack(VideoStreamTrack):
    def __init__(self, app):
        super().__init__()
        self.app = app

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        frame = self.app.latest_local_frame
        
        if not self.app.cam_enabled or frame is None:
            frame = np.zeros((240, 320, 3), dtype=np.uint8)
            cv2.putText(frame, "Kamera Kapali", (40, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        else:
            frame = cv2.resize(frame, (320, 240)) # İnterneti yormayan altın çözünürlük
            
        new_frame = VideoFrame.from_ndarray(frame, format="bgr24")
        new_frame.pts = pts
        new_frame.time_base = time_base
        await asyncio.sleep(0.04) # Saniyede pürüzsüz 25 kare akış hızı
        return new_frame

class ProMeetApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PRO MEET - WhatsApp Premium")
        
        self.screen_w = self.root.winfo_screenwidth()
        self.screen_h = self.root.winfo_screenheight()
        self.root.geometry(f"{self.screen_w}x{self.screen_h}+0+0")
        self.root.overrideredirect(True)
        self.root.configure(fg_color='#0b141a')
        
        self.root.bind("<Escape>", lambda e: self.on_closing())
        
        self.loop = asyncio.new_event_loop()
        self.pc = None
        self.latest_local_frame = None
        self.latest_remote_frame = None
        
        # Zırhlı Ses Depoları
        self.audio_in_queue = b""
        self.audio_out_queue = b""
        
        self.mic_enabled = True
        self.cam_enabled = True
        self.is_call_active = False
        self.is_connected = False
        self.role = None
        
        self.my_name = "Kullanici_" + str(np.random.randint(10, 99))
        self.selected_target_user = None
        self.incoming_call_panel = None
        
        # 🔊 48000Hz Endüstriyel Sürücü Standartı (Gecikmesiz Hat)
        self.pya = pyaudio.PyAudio()
        try:
            self.in_stream = self.pya.open(format=pyaudio.paInt16, channels=1, rate=48000, input=True, frames_per_buffer=960)
            self.out_stream = self.pya.open(format=pyaudio.paInt16, channels=1, rate=48000, output=True)
        except:
            self.in_stream = None
            self.out_stream = None
            print("Ses donanımı başlatılamadı.")
        
        self.cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        if not self.cap.isOpened(): self.cap = cv2.VideoCapture(1, cv2.CAP_DSHOW)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)

        self.setup_whatsapp_main_ui()
        
        # İzole Donanım İşçileri (Asla Ana Döngüyü Engellemezler)
        threading.Thread(target=self.audio_input_thread, daemon=True).start()
        threading.Thread(target=self.audio_output_thread, daemon=True).start()
        
        self.safe_camera_and_ui_loop()
        threading.Thread(target=self.start_async_loop, daemon=True).start()
        threading.Thread(target=self.bg_firebase_sync_thread, daemon=True).start()

    def setup_whatsapp_main_ui(self):
        self.root.grid_columnconfigure(0, weight=0, minsize=320) 
        self.root.grid_columnconfigure(1, weight=1)             
        self.root.grid_columnconfigure(2, weight=0, minsize=350) 
        self.root.grid_rowconfigure(0, weight=1)

        # 1. SOL PANEL
        self.left_panel = ctk.CTkFrame(self.root, corner_radius=0, fg_color="#111b21", border_width=1, border_color="#222d34")
        self.left_panel.grid(row=0, column=0, sticky="nsew")
        
        self.profile_bar = ctk.CTkFrame(self.left_panel, height=60, corner_radius=0, fg_color="#202c33")
        self.profile_bar.pack(fill="x", side="top")
        
        self.user_entry = ctk.CTkEntry(self.profile_bar, width=140, height=32, corner_radius=6, fg_color="#2a3942", border_width=0, text_color="white")
        self.user_entry.pack(side="left", padx=10, pady=14)
        self.user_entry.insert(0, self.my_name)

        self.btn_register_presence = ctk.CTkButton(self.profile_bar, text="Aktif Et", width=65, height=32, fg_color="#00a884", font=ctk.CTkFont(size=11, weight="bold"), command=self.register_presence_action)
        self.btn_register_presence.pack(side="left", padx=5)

        ctk.CTkLabel(self.left_panel, text="ÇEVRİMİÇİ KİŞİLER", font=ctk.CTkFont(size=11, weight="bold"), text_color="#8696a0").pack(anchor="w", padx=15, pady=(15, 5))
        self.contacts_scroll_frame = ctk.CTkScrollableFrame(self.left_panel, fg_color="#111b21", label_text="")
        self.contacts_scroll_frame.pack(fill="both", expand=True, padx=10, pady=5)

        # 2. ORTA PANEL
        self.center_panel = ctk.CTkFrame(self.root, corner_radius=0, fg_color="#0b141a")
        self.center_panel.grid(row=0, column=1, sticky="nsew", padx=15, pady=15)
        
        self.lbl_main_placeholder = ctk.CTkLabel(self.center_panel, text="Arama başlatmak için soldan birini seçip üstteki 'Ara' butonuna basın.", font=ctk.CTkFont(size=14), text_color="gray")
        self.lbl_main_placeholder.place(relx=0.5, rely=0.5, anchor="center")

        # Çağrı Katmanı
        self.outgoing_call_panel = ctk.CTkFrame(self.center_panel, fg_color="#0b141a", corner_radius=0)
        self.call_box = ctk.CTkFrame(self.outgoing_call_panel, width=420, height=260, fg_color="#111b21", corner_radius=16, border_width=1, border_color="#00a884")
        self.call_box.place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkLabel(self.call_box, text="📹 GÖRÜNTÜLÜ ARAMA", font=ctk.CTkFont(size=12, weight="bold"), text_color="#00a884").place(relx=0.5, y=35, anchor="center")
        self.lbl_call_target_name = ctk.CTkLabel(self.call_box, text="Kullanıcı", font=ctk.CTkFont(size=24, weight="bold"), text_color="white")
        self.lbl_call_target_name.place(relx=0.5, y=90, anchor="center")
        self.lbl_call_phase = ctk.CTkLabel(self.call_box, text="Aranıyor...", font=ctk.CTkFont(size=18, weight="bold", slant="italic"), text_color="#34b7f1")
        self.lbl_call_phase.place(relx=0.5, y=140, anchor="center")
        ctk.CTkButton(self.call_box, text="🛑 İptal Et", width=220, height=40, corner_radius=20, fg_color="#ea0038", font=ctk.CTkFont(weight="bold"), command=self.cancel_outgoing_call_action).place(relx=0.5, y=200, anchor="center")

        # Video Görüntüleme Katmanı
        self.video_display_panel = ctk.CTkFrame(self.center_panel, fg_color="transparent")
        self.video_display_panel.grid_rowconfigure(0, weight=1)
        self.video_display_panel.grid_columnconfigure(0, weight=1)
        self.video_display_panel.grid_columnconfigure(1, weight=1)
        self.local_video_label = ctk.CTkLabel(self.video_display_panel, text="Kameranız", fg_color="#111b21", corner_radius=12)
        self.local_video_label.grid(row=0, column=0, padx=10, pady=(10, 80), sticky="nsew")
        self.remote_video_label = ctk.CTkLabel(self.video_display_panel, text="Canlı Yayın Bekleniyor...", fg_color="#111b21", corner_radius=12)
        self.remote_video_label.grid(row=0, column=1, padx=10, pady=(10, 80), sticky="nsew")
        
        self.control_bar = ctk.CTkFrame(self.video_display_panel, height=60, fg_color="transparent")
        self.control_bar.place(relx=0.5, rely=0.92, anchor="center")
        self.btn_mic = ctk.CTkButton(self.control_bar, text="🎤 Mik: Açık", width=110, height=38, corner_radius=8, fg_color="#2a3942", text_color="#00a884", command=self.toggle_mic)
        self.btn_mic.pack(side="left", padx=5)
        self.btn_cam = ctk.CTkButton(self.control_bar, text="📷 Kam: Açık", width=110, height=38, corner_radius=8, fg_color="#2a3942", text_color="#00a884", command=self.toggle_cam)
        self.btn_cam.pack(side="left", padx=5)
        ctk.CTkButton(self.control_bar, text="🛑 Kapat", width=110, height=38, corner_radius=8, fg_color="#ea0038", text_color="white", font=ctk.CTkFont(weight="bold"), command=self.on_closing).pack(side="left", padx=5)

        # 3. SAĞ PANEL
        self.right_panel = ctk.CTkFrame(self.root, corner_radius=0, fg_color="#111b21", border_width=1, border_color="#222d34")
        self.right_panel.grid(row=0, column=2, sticky="nsew")
        self.right_panel.grid_rowconfigure(1, weight=1)

        self.chat_bar = ctk.CTkFrame(self.right_panel, height=60, corner_radius=0, fg_color="#202c33")
        self.chat_bar.grid(row=0, column=0, sticky="ew")
        self.lbl_chat_title = ctk.CTkLabel(self.chat_bar, text="💬 SOHBET ODASI", font=ctk.CTkFont(size=14, weight="bold"), text_color="#e9edef")
        self.lbl_chat_title.pack(side="left", padx=15, pady=18)

        self.btn_call = ctk.CTkButton(self.chat_bar, text="📹 Ara", width=70, height=34, corner_radius=17, fg_color="#00a884", state="disabled", font=ctk.CTkFont(weight="bold"), command=self.start_server_action)
        self.btn_call.pack(side="right", padx=15, pady=13)

        self.chat_box = ctk.CTkTextbox(self.right_panel, fg_color="#0b141a", border_width=1, border_color="#222d34", font=("Segoe UI", 12), text_color="#e9edef")
        self.chat_box.grid(row=1, column=0, sticky="nsew", padx=15, pady=10)
        self.chat_box.configure(state="disabled")

        self.bottom_bar = ctk.CTkFrame(self.right_panel, height=60, fg_color="transparent")
        self.bottom_bar.grid(row=2, column=0, sticky="ew", padx=10, pady=10)
        self.msg_entry = ctk.CTkEntry(self.bottom_bar, height=38, placeholder_text="Bir mesaj yazın...", fg_color="#2a3942", border_width=0, text_color="white")
        self.msg_entry.pack(side="left", fill="x", expand=True, padx=(5, 5))
        self.msg_entry.bind("<Return>", lambda e: self.send_message())
        ctk.CTkButton(self.bottom_bar, text="Gönder", width=65, height=38, fg_color="#00a884", text_color="white", font=ctk.CTkFont(weight="bold"), command=self.send_message).pack(side="right", padx=(5, 5))

    def register_presence_action(self):
        new_name = self.user_entry.get().strip()
        if new_name:
            try:
                db.reference('active_users').child(self.my_name).delete()
                self.my_name = new_name
                db.reference('active_users').child(self.my_name).set(time.time())
            except: pass

    def select_user_target(self, username):
        self.selected_target_user = username
        self.lbl_chat_title.configure(text=f"💬 {username} ile Sohbet")
        self.btn_call.configure(state="normal", text=f"📹 {username}'ı Ara")

    def show_outgoing_call_screen(self, target_name):
        self.lbl_main_placeholder.place_forget()
        self.video_display_panel.pack_forget()
        self.lbl_call_target_name.configure(text=target_name)
        self.lbl_call_phase.configure(text="Aranıyor...", text_color="#34b7f1")
        self.outgoing_call_panel.pack(fill="both", expand=True)

    def cancel_outgoing_call_action(self):
        try:
            db.reference('webrtc_call').delete()
            self.outgoing_call_panel.pack_forget()
            self.video_display_panel.pack_forget()
            self.is_call_active = False
            self.lbl_main_placeholder.place(relx=0.5, rely=0.5, anchor="center")
        except: pass

    def show_incoming_call_popup(self, caller_name):
        if self.incoming_call_panel is not None: return
        self.incoming_call_panel = ctk.CTkFrame(self.root, width=420, height=220, fg_color="#202c33", border_width=2, border_color="#00a884", corner_radius=16)
        self.incoming_call_panel.place(relx=0.5, rely=0.3, anchor="center")
        ctk.CTkLabel(self.incoming_call_panel, text="📞 GELEN GÖRÜNTÜLÜ ARAMA", font=ctk.CTkFont(size=13, weight="bold"), text_color="#00a884").place(x=30, y=25)
        ctk.CTkLabel(self.incoming_call_panel, text=f"{caller_name} arıyor...", font=ctk.CTkFont(size=20, weight="bold"), text_color="white").place(x=30, y=65)
        ctk.CTkLabel(self.incoming_call_panel, text="Görüşmeyi kabul etmek için onaylayın.", font=ctk.CTkFont(size=12), text_color="gray").place(x=30, y=100)
        ctk.CTkButton(self.incoming_call_panel, text="✅ Kabul Et", width=160, height=40, corner_radius=20, fg_color="#00a884", font=ctk.CTkFont(weight="bold"), command=lambda: self.accept_call_action(caller_name)).place(x=30, y=145)
        ctk.CTkButton(self.incoming_call_panel, text="❌ Reddet", width=160, height=40, corner_radius=20, fg_color="#ea0038", font=ctk.CTkFont(weight="bold"), command=self.reject_call_action).place(x=220, y=145)
        
        db.reference('webrtc_call').child('ring_status').set('ringing')

    def accept_call_action(self, caller_name):
        if self.incoming_call_panel:
            self.incoming_call_panel.destroy()
            self.incoming_call_panel = None
        self.role = 'client'
        self.lbl_main_placeholder.place_forget()
        self.outgoing_call_panel.pack_forget()
        self.video_display_panel.pack(fill="both", expand=True)
        asyncio.run_coroutine_threadsafe(self.client_logic(), self.loop)

    def reject_call_action(self):
        try:
            db.reference('webrtc_call_answer').set({'status': 'rejected'})
            if self.incoming_call_panel:
                self.incoming_call_panel.destroy()
                self.incoming_call_panel = None
            self.is_call_active = False
        except: pass

    def secure_polling_loop(self):
        try:
            if self.role == 'server' and not self.is_connected:
                call_node = db.reference('webrtc_call').get()
                if call_node and call_node.get('ring_status') == 'ringing':
                    self.lbl_call_phase.configure(text="Çalıyor...", text_color="#00a884")

                ans_data = db.reference('webrtc_call_answer').get()
                if ans_data:
                    if ans_data.get('status') == 'rejected':
                        self.cancel_outgoing_call_action()
                        return
                    if ans_data.get('sdp'):
                        async def set_answer():
                            try:
                                await self.pc.setRemoteDescription(RTCSessionDescription(sdp=ans_data['sdp'], type=ans_data['type']))
                                self.is_connected = True
                                self.outgoing_call_panel.pack_forget()
                                self.video_display_panel.pack(fill="both", expand=True)
                            except: pass
                        asyncio.run_coroutine_threadsafe(set_answer(), self.loop)

            target_node = 'client_ice' if self.role == 'server' else 'server_ice'
            ice_data = db.reference(target_node).get()
            if ice_data and isinstance(ice_data, dict):
                for ice_id, ice in ice_data.items():
                    try:
                        cand = RTCIceCandidate(candidate=ice['candidate'], sdpMid=ice['sdpMid'], sdpMLineIndex=ice['sdpMLineIndex'])
                        asyncio.run_coroutine_threadsafe(self.pc.addIceCandidate(cand), self.loop)
                    except: pass
        except: pass
        self.root.after(400, self.secure_polling_loop)

    # 🎙️ [İZOLASYON GİRİŞİ]: Mikrofondan gelen byte'ları kilitlenmeyen kuyruğa yazar
    def audio_input_thread(self):
        while True:
            try:
                if self.in_stream:
                    raw_data = self.in_stream.read(960, exception_on_overflow=False)
                    self.audio_in_queue += raw_data
                else:
                    time.sleep(0.01)
                
                # Bellek taşma bariyeri
                if len(self.audio_in_queue) > 19200:
                    self.audio_in_queue = self.audio_in_queue[-19200:]
            except: pass

    # 🔊 [İZOLASYON ÇIKIŞI]: Karşıdan gelen Opus çözülmüş sesleri hoparlöre basar
    def audio_output_thread(self):
        while True:
            try:
                if self.out_stream and len(self.audio_out_queue) >= 1920:
                    chunk = self.audio_out_queue[:1920]
                    self.audio_out_queue = self.audio_out_queue[1920:]
                    self.out_stream.write(chunk)
                else:
                    time.sleep(0.01)
            except: pass

    def bg_firebase_sync_thread(self):
        while True:
            try:
                db.reference('active_users').child(self.my_name).set(time.time())
                chat_data = db.reference('webrtc_chat').get()
                if chat_data: self.root.after(0, lambda: self.render_chat_box(chat_data))
                user_data = db.reference('active_users').get()
                if user_data: self.root.after(0, lambda: self.render_contacts(user_data))

                if not self.is_call_active:
                    call_offer = db.reference('webrtc_call').get()
                    if call_offer and call_offer.get('caller') != self.my_name and call_offer.get('target') == self.my_name:
                        self.is_call_active = True
                        self.root.after(0, lambda: self.show_incoming_call_popup(call_offer['caller']))
            except: pass
            time.sleep(1.0)

    def render_chat_box(self, data):
        try:
            self.chat_box.configure(state="normal")
            self.chat_box.delete("1.0", "end")
            for key, val in data.items():
                self.chat_box.insert("end", f"{val['user']}: {val['text']}\n")
            self.chat_box.configure(state="disabled")
            self.chat_box.see("end")
        except: pass

    def render_contacts(self, data):
        try:
            for widget in self.contacts_scroll_frame.winfo_children(): widget.destroy()
            now = time.time()
            for username, last_seen in data.items():
                if username == self.my_name: continue
                if now - float(last_seen) > 20: continue
                btn = ctk.CTkButton(self.contacts_scroll_frame, text=f"🟢 {username} (Çevrimiçi)", anchor="w", fg_color="#202c33", hover_color="#2a3942", command=lambda u=username: self.select_user_target(u))
                btn.pack(fill="x", padx=5, pady=4)
        except: pass

    def safe_camera_and_ui_loop(self):
        if self.cap.isOpened():
            ret, frame = self.cap.read()
            if ret: self.latest_local_frame = frame
            
            if self.video_display_panel.winfo_manager() and self.local_video_label is not None and frame is not None:
                frame_resized = cv2.resize(frame, (400, 300))
                if self.cam_enabled:
                    rgb_frame = cv2.cvtColor(frame_resized, cv2.COLOR_BGR2RGB)
                    ctk_img = ctk.CTkImage(light_image=Image.fromarray(rgb_frame), dark_image=Image.fromarray(rgb_frame), size=(400, 300))
                    self.local_video_label.configure(image=ctk_img, text="")
                else:
                    self.local_video_label.configure(image='', text="Kamera Kapalı", text_color="red")
        
        if self.is_connected and self.latest_remote_frame is not None and self.remote_video_label is not None:
            try:
                remote_resized = cv2.resize(self.latest_remote_frame, (400, 300))
                ctk_remote_img = ctk.CTkImage(light_image=Image.fromarray(cv2.cvtColor(remote_resized, cv2.COLOR_BGR2RGB)), dark_image=Image.fromarray(cv2.cvtColor(remote_resized, cv2.COLOR_BGR2RGB)), size=(400, 300))
                self.remote_video_label.configure(image=ctk_remote_img, text="")
            except: pass

        self.root.after(45, self.safe_camera_and_ui_loop)

    def get_rtc_configuration(self):
        return RTCConfiguration(iceServers=[
            RTCIceServer(urls="stun:stun.l.google.com:19302"),
            RTCIceServer(urls="turn:openrelay.metered.ca:443", username="openrelay", credential="openrelay")
        ])

    def start_server_action(self):
        if not self.selected_target_user: return
        if self.is_call_active: return
        self.role = 'server'
        self.is_call_active = True
        self.show_outgoing_call_screen(self.selected_target_user)
        asyncio.run_coroutine_threadsafe(self.server_logic(), self.loop)

    def toggle_mic(self):
        self.mic_enabled = not self.mic_enabled
        self.btn_mic.configure(text="🎤 Mik: Açık" if self.mic_enabled else "🔇 Mik: Kapalı", text_color="#00a884" if self.mic_enabled else "red")

    def toggle_cam(self):
        self.cam_enabled = not self.cam_enabled
        self.btn_cam.configure(text="📷 Kam: Açık" if self.cam_enabled else "❌ Kam: Kapalı", text_color="#00a884" if self.cam_enabled else "red")

    def send_message(self):
        msg = self.msg_entry.get().strip()
        if msg:
            try:
                db.reference('webrtc_chat').push().set({'user': self.my_name, 'text': msg})
                self.msg_entry.delete(0, "end")
            except Exception as e: print("Mesaj hatası:", e)

    def start_async_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def server_logic(self):
        config = self.get_rtc_configuration()
        self.pc = RTCPeerConnection(configuration=config)
        self.pc.addTrack(TkinterVideoTrack(self))
        self.pc.addTrack(LocalAudioTrack(self)) # Gerçek Ses Kanalları Mühürlendi!

        @self.pc.on("icecandidate")
        def on_icecandidate(candidate):
            if candidate:
                node = 'server_ice' if self.role == 'server' else 'client_ice'
                db.reference(node).push().set({
                    'candidate': candidate.candidate, 'sdpMid': candidate.sdpMid, 'sdpMLineIndex': candidate.sdpMLineIndex
                })

        @self.pc.on("track")
        def on_track(track):
            if track.kind == "video":
                async def display_remote():
                    while True:
                        try:
                            frame = await track.recv()
                            self.latest_remote_frame = frame.to_ndarray(format="bgr24")
                        except: break
                asyncio.ensure_future(display_remote(), loop=self.loop)
            elif track.kind == "audio":
                async def play_audio():
                    while True:
                        try:
                            frame = await track.recv()
                            self.audio_out_queue += frame.to_ndarray().tobytes()
                        except: break
                asyncio.ensure_future(play_audio(), loop=self.loop)

        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        
        db.reference('webrtc_call').set({
            'sdp': self.pc.localDescription.sdp, 
            'type': self.pc.localDescription.type,
            'caller': self.my_name,
            'target': self.selected_target_user,
            'ring_status': 'calling'
        })
        self.root.after(400, self.secure_polling_loop)

    async def client_logic(self):
        config = self.get_rtc_configuration()
        self.pc = RTCPeerConnection(configuration=config)
        self.pc.addTrack(TkinterVideoTrack(self))
        self.pc.addTrack(LocalAudioTrack(self))

        @self.pc.on("icecandidate")
        def on_icecandidate(candidate):
            if candidate:
                node = 'server_ice' if self.role == 'server' else 'client_ice'
                db.reference(node).push().set({
                    'candidate': candidate.candidate, 'sdpMid': candidate.sdpMid, 'sdpMLineIndex': candidate.sdpMLineIndex
                })

        @self.pc.on("track")
        def on_track(track):
            if track.kind == "video":
                async def display_remote():
                    while True:
                        try:
                            frame = await track.recv()
                            self.latest_remote_frame = frame.to_ndarray(format="bgr24")
                        except: break
                asyncio.ensure_future(display_remote(), loop=self.loop)
            elif track.kind == "audio":
                async def play_audio():
                    while True:
                        try:
                            frame = await track.recv()
                            self.audio_out_queue += frame.to_ndarray().tobytes()
                        except: break
                asyncio.ensure_future(play_audio(), loop=self.loop)

        offer_data = db.reference('webrtc_call').get()
        if not offer_data: return
        
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=offer_data['sdp'], type=offer_data['type']))
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        db.reference('webrtc_call_answer').set({'sdp': self.pc.localDescription.sdp, 'type': self.pc.localDescription.type, 'status': 'accepted'})
        
        self.is_connected = True
        self.root.after(400, self.secure_polling_loop)

    def on_closing(self):
        try: self.loop.stop()
        except: pass
        if self.cap.isOpened(): self.cap.release()
        try:
            if self.in_stream: self.in_stream.close()
            if self.out_stream: self.out_stream.close()
            self.pya.terminate()
        except: pass
        try:
            db.reference('active_users').child(self.my_name).delete()
            db.reference('server_ice').delete()
            db.reference('client_ice').delete()
            db.reference('webrtc_call').delete()
            db.reference('webrtc_call_answer').delete()
        except: pass
        self.root.destroy()

if __name__ == "__main__":
    root = ctk.CTk()
    app = ProMeetApp(root)
    root.mainloop()