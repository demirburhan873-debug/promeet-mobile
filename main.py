import os
import sys
import random
import time
import threading
import asyncio
import cv2
import numpy as np

import firebase_admin
from firebase_admin import credentials, db

from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, AudioStreamTrack, RTCConfiguration, RTCIceServer, RTCIceCandidate
from av import VideoFrame, AudioFrame

# Kivy Mobil Grafik Motoru Bileşenleri
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.uix.scrollview import ScrollView
from kivy.graphics.texture import Texture
from kivy.clock import Clock

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

class KivyVideoTrack(VideoStreamTrack):
    def __init__(self, app_instance):
        super().__init__()
        self.app_instance = app_instance

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        frame = self.app_instance.latest_local_frame
        if frame is None or not self.app_instance.cam_enabled:
            frame = np.zeros((240, 320, 3), dtype=np.uint8)
            cv2.putText(frame, "Kamera Kapali", (40, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        else:
            frame = cv2.resize(frame, (320, 240))
        new_frame = VideoFrame.from_ndarray(frame, format="bgr24")
        new_frame.pts = pts
        new_frame.time_base = time_base
        await asyncio.sleep(0.04)
        return new_frame

class ProMeetMobileApp(App):
    def build(self):
        self.title = "PRO MEET - Mobile Enterprise"
        
        # Temel Değişkenler
        self.loop = asyncio.new_event_loop()
        self.pc = None
        self.latest_local_frame = None
        self.latest_remote_frame = None
        self.audio_in_queue = b""
        self.audio_out_queue = b""
        self.mic_enabled = True
        self.cam_enabled = True
        self.is_call_active = False
        self.is_connected = False
        self.role = None
        self.selected_target_user = None
        
        self.my_name = "Mobil_" + str(random.randint(10, 99))
        
        # Mobil Kamera Başlatıcı
        self.cap = cv2.VideoCapture(0)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
        
        # 📲 ANA MOBİL ARAYÜZ DÜZENİ
        self.main_layout = BoxLayout(orientation='vertical', spacing=10, padding=10)
        
        # Üst Profil ve Aktif Et Barı
        top_bar = BoxLayout(size_hint_y=None, height=50, spacing=10)
        self.txt_username = TextInput(text=self.my_name, multiline=False, background_color=(0.16, 0.22, 0.26, 1), foreground_color=(1, 1, 1, 1))
        btn_active = Button(text="Aktif Et", size_hint_x=None, width=100, background_color=(0, 0.65, 0.51, 1))
        btn_active.bind(on_press=self.register_presence_action)
        top_bar.add_widget(self.txt_username)
        top_bar.add_widget(btn_active)
        self.main_layout.add_widget(top_bar)
        
        # Orta Alan: İkiye Bölünen Ekran (Sol Liste, Sağ Kamera/Sohbet)
        content_area = BoxLayout(orientation='horizontal', spacing=10)
        
        # Çevrimiçi Kişiler Kaydırma Alanı
        scroll = ScrollView(size_hint_x=0.4)
        self.contacts_layout = GridLayout(cols=1, spacing=5, size_hint_y=None)
        self.contacts_layout.bind(minimum_height=self.contacts_layout.setter('height'))
        scroll.add_widget(self.contacts_layout)
        content_area.add_widget(scroll)
        
        # Sağ Canlı Video Gösterim Kutuları
        self.video_box = BoxLayout(orientation='vertical', spacing=5)
        self.lbl_status = Label(text="Aramak için soldan birini seçip\n'Ara' butonuna basın.", halign="center")
        self.video_box.add_widget(self.lbl_status)
        
        # Kameraların Çizileceği Mobil Label Alanları
        self.img_local = Label(text="[ Yerel Kamera ]", size_hint_y=0.4)
        self.img_remote = Label(text="[ Karşı Taraf ]", size_hint_y=0.4)
        
        content_area.add_widget(self.video_box)
        self.main_layout.add_widget(content_area)
        
        # Alt Çağrı Yönetim Butonları
        self.bottom_bar = BoxLayout(size_hint_y=None, height=60, spacing=10)
        self.btn_call = Button(text="📹 Ara", disabled=True, background_color=(0, 0.65, 0.51, 1))
        self.btn_call.bind(on_press=self.start_server_action)
        self.btn_close = Button(text="🛑 Kapat", background_color=(0.91, 0, 0.22, 1))
        self.btn_close.bind(on_press=self.on_closing)
        
        self.bottom_bar.add_widget(self.btn_call)
        self.bottom_bar.add_widget(self.btn_close)
        self.main_layout.add_widget(self.bottom_bar)
        
        # Zamanlayıcıları (Kamera ve Ağ İşçilerini) Ateşle
        Clock.schedule_interval(self.update_camera_and_ui, 1.0 / 25.0)
        threading.Thread(target=self.start_async_loop, daemon=True).start()
        threading.Thread(target=self.bg_firebase_sync_thread, daemon=True).start()
        
        return self.main_layout

    def register_presence_action(self, instance):
        new_name = self.txt_username.text.strip()
        if new_name:
            db.reference('active_users').child(self.my_name).delete()
            self.my_name = new_name
            db.reference('active_users').child(self.my_name).set(time.time())

    def select_user_target(self, username):
        self.selected_target_user = username
        self.btn_call.disabled = False
        self.btn_call.text = f"📹 {username}'ı Ara"

    # 📲 [MOBİL GÖRÜNTÜ VE ARAYÜZ MOTORU]: Kasmaları bitiren pürüzsüz çizim döngüsü
    def update_camera_and_ui(self, dt):
        if self.cap.isOpened():
            ret, frame = self.cap.read()
            if ret:
                self.latest_local_frame = frame
                if self.is_call_active and self.cam_enabled:
                    # Ham matrisi Kivy dokusuna (Texture) çevirip ekrana bas
                    buf = cv2.flip(frame, 0).tobytes()
                    texture = Texture.create(size=(frame.shape[1], frame.shape[0]), colorfmt='bgr')
                    texture.blit_buffer(buf, colorfmt='bgr', bufferfmt='ubyte')
                    self.img_local.texture = texture

        if self.is_connected and self.latest_remote_frame is not None:
            try:
                r_frame = self.latest_remote_frame
                buf_r = cv2.flip(r_frame, 0).tobytes()
                tex_r = Texture.create(size=(r_frame.shape[1], r_frame.shape[0]), colorfmt='bgr')
                tex_r.blit_buffer(buf_r, colorfmt='bgr', bufferfmt='ubyte')
                self.img_remote.texture = tex_r
            except: pass

    def bg_firebase_sync_thread(self):
        while True:
            try:
                db.reference('active_users').child(self.my_name).set(time.time())
                user_data = db.reference('active_users').get()
                if user_data:
                    Clock.schedule_once(lambda dt: self.update_contacts_list(user_data))

                # Gelen Arama Sorgusu
                if not self.is_call_active:
                    call_offer = db.reference('webrtc_call').get()
                    if call_offer and call_offer.get('caller') != self.my_name and call_offer.get('target') == self.my_name:
                        self.is_call_active = True
                        Clock.schedule_once(lambda dt: self.show_incoming_call_popup(call_offer['caller']))
            except: pass
            time.sleep(1.2)

    def update_contacts_list(self, data):
        self.contacts_layout.clear_widgets()
        now = time.time()
        for username, last_seen in data.items():
            if username == self.my_name: continue
            if now - float(last_seen) > 20: continue
            btn = Button(text=f"🟢 {username}", size_hint_y=None, height=44, background_color=(0.12, 0.17, 0.20, 1))
            btn.bind(on_press=lambda inst, u=username: self.select_user_target(u))
            self.contacts_layout.add_widget(btn)

    def show_incoming_call_popup(self, caller_name):
        # Mobil uyumlu arama kutusunu orta alana yerleştir
        self.video_box.clear_widgets()
        lbl_info = Label(text=f"📞 Gelen Arama!\n{caller_name} arıyor...", font_size=18, halign="center")
        btn_accept = Button(text="✅ Kabul Et", background_color=(0, 0.65, 0.51, 1), size_hint_y=None, height=50)
        btn_accept.bind(on_press=lambda inst: self.accept_call_action())
        
        self.video_box.add_widget(lbl_info)
        self.video_box.add_widget(btn_accept)
        db.reference('webrtc_call').child('ring_status').set('ringing')

    def accept_call_action(self):
        self.video_box.clear_widgets()
        self.video_box.add_widget(self.img_local)
        self.video_box.add_widget(self.img_remote)
        self.role = 'client'
        asyncio.run_coroutine_threadsafe(self.client_logic(), self.loop)

    def secure_polling_loop(self):
        try:
            if self.role == 'server' and not self.is_connected:
                call_node = db.reference('webrtc_call').get()
                if call_node and call_node.get('ring_status') == 'ringing':
                    self.lbl_status.text = "Çalıyor..."

                ans_data = db.reference('webrtc_call_answer').get()
                if ans_data and ans_data.get('sdp'):
                    async def set_answer():
                        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=ans_data['sdp'], type=ans_data['type']))
                        self.is_connected = True
                        self.video_box.clear_widgets()
                        self.video_box.add_widget(self.img_local)
                        self.video_box.add_widget(self.img_remote)
                    asyncio.run_coroutine_threadsafe(set_answer(), self.loop)

            # ICE Paket Eşitlemesi
            target_node = 'client_ice' if self.role == 'server' else 'server_ice'
            ice_data = db.reference(target_node).get()
            if ice_data and isinstance(ice_data, dict):
                for ice_id, ice in ice_data.items():
                    cand = RTCIceCandidate(candidate=ice['candidate'], sdpMid=ice['sdpMid'], sdpMLineIndex=ice['sdpMLineIndex'])
                    asyncio.run_coroutine_threadsafe(self.pc.addIceCandidate(cand), self.loop)
        except: pass
        if not self.is_connected:
            Clock.schedule_once(lambda dt: self.secure_polling_loop(), 0.5)

    def start_async_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start_server_action(self, instance):
        self.role = 'server'
        self.is_call_active = True
        self.lbl_status.text = "Aranıyor..."
        asyncio.run_coroutine_threadsafe(self.server_logic(), self.loop)

    async def server_logic(self):
        try:
            db.reference('webrtc_call').delete()
            db.reference('webrtc_call_answer').delete()
            db.reference('server_ice').delete()
            db.reference('client_ice').delete()
        except: pass

        self.pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[RTCIceServer(urls="stun:stun.l.google.com:19302")]))
        self.pc.addTrack(KivyVideoTrack(self))

        @self.pc.on("icecandidate")
        def on_icecandidate(candidate):
            if candidate:
                node = 'server_ice' if self.role == 'server' else 'client_ice'
                db.reference(node).push().set({'candidate': candidate.candidate, 'sdpMid': candidate.sdpMid, 'sdpMLineIndex': candidate.sdpMLineIndex})

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

        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        
        db.reference('webrtc_call').set({
            'sdp': self.pc.localDescription.sdp, 'type': self.pc.localDescription.type,
            'caller': self.my_name, 'target': self.selected_target_user, 'ring_status': 'calling'
        })
        Clock.schedule_once(lambda dt: self.secure_polling_loop(), 0.4)

    async def client_logic(self):
        self.pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[RTCIceServer(urls="stun:stun.l.google.com:19302")]))
        self.pc.addTrack(KivyVideoTrack(self))

        @self.pc.on("icecandidate")
        def on_icecandidate(candidate):
            if candidate:
                node = 'server_ice' if self.role == 'server' else 'client_ice'
                db.reference(node).push().set({'candidate': candidate.candidate, 'sdpMid': candidate.sdpMid, 'sdpMLineIndex': candidate.sdpMLineIndex})

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

        offer_data = db.reference('webrtc_call').get()
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=offer_data['sdp'], type=offer_data['type']))
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        db.reference('webrtc_call_answer').set({'sdp': self.pc.localDescription.sdp, 'type': self.pc.localDescription.type, 'status': 'accepted'})
        
        self.is_connected = True
        Clock.schedule_once(lambda dt: self.secure_polling_loop(), 0.4)

    def on_closing(self, *args):
        try: self.loop.stop()
        except: pass
        if self.cap.isOpened(): self.cap.release()
        try: db.reference('active_users').child(self.my_name).delete()
        except: pass
        self.stop()

if __name__ == "__main__":
    ProMeetMobileApp().run()