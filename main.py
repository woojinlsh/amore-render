from fastapi import FastAPI, Request
import asyncio
import httpx
import time
import os
import redis.asyncio as redis

app = FastAPI()

# 환경변수 불러오기
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
VERKADA_API_KEY = os.getenv("VERKADA_API_KEY")
TARGET_CAMERA_ID = os.getenv("TARGET_CAMERA_ID")
ORG_ID = os.getenv("ORG_ID", "607ef9ff-2910-4a68-bfe6-318836f42d12")

# 허용할 카메라 ID 목록
ALLOWED_CAMERAS_STR = os.getenv("ALLOWED_CAMERA_IDS", "")
ALLOWED_CAMERAS = [cam.strip() for cam in ALLOWED_CAMERAS_STR.split(",") if cam.strip()]

redis_client = redis.from_url(REDIS_URL, decode_responses=True)

# 쿨다운 타임 설정 (15초)
COOLDOWN_MS = 15000 

@app.post("/webhook")
async def handle_webhook(request: Request):
    payload = await request.json()
    
    event_category = "unknown"
    if payload.get("type") == "heartbeat":
        event_category = "heartbeat"
    elif payload.get("type") == "switch":
        event_category = "switch"
    elif payload.get("webhook_type") == "notification" or (payload.get("data") and payload.get("data").get("device_type") == "camera"):
        event_category = "line_crossing"

    # 1. Heartbeat 처리
    if event_category == "heartbeat":
        await redis_client.set("last_heartbeat", int(time.time() * 1000))
        return {"message": "Heartbeat updated"}

    # 2. Switch 처리
    if event_category == "switch":
        await redis_client.set("switch_status", "1")
        return {"message": "Switch event recorded"}

    # 3. Line Crossing 처리
    if event_category == "line_crossing":
        
        # 들어온 웹훅 데이터에서 카메라 ID 추출 (없을 경우 "Unknown" 처리)
        incoming_camera_id = payload.get("data", {}).get("camera_id", "Unknown")
        
        # 카메라 ID 필터링 로직
        if ALLOWED_CAMERAS and incoming_camera_id not in ALLOWED_CAMERAS:
            print(f"필터링됨: 허용되지 않은 카메라의 알림입니다. (ID: {incoming_camera_id})")
            return {"message": f"Camera {incoming_camera_id} is not in allowed list. Ignored."}

        now = int(time.time() * 1000)
        
        # 쿨다운 로직
        last_crossing_str = await redis_client.get("last_line_crossing")
        last_crossing = int(last_crossing_str) if last_crossing_str else 0
        
        if now - last_crossing < COOLDOWN_MS:
            print("쿨다운 적용 중: 중복 Line Crossing 이벤트 무시됨")
            return {"message": "Cooldown active. Ignored."}
            
        await redis_client.set("last_line_crossing", now)

        last_heartbeat_str = await redis_client.get("last_heartbeat")
        last_heartbeat = int(last_heartbeat_str) if last_heartbeat_str else 0
        
        helix_attributes = {}

        # ★ [수정됨] 조건에 따른 Helix Payload에 "Camera ID" 필드 추가
        if now - last_heartbeat > 10000:
            helix_attributes = {
                "Smart Relay": "Off", 
                "Line Crossing": "detected", 
                "Brake": "FailtoBrake",
                "Camera ID": incoming_camera_id
            }
        else:
            await redis_client.set("switch_status", "0")
            await asyncio.sleep(5) 
            
            switch_status = await redis_client.get("switch_status")
            if switch_status == "1":
                helix_attributes = {
                    "Smart Relay": "On", 
                    "Line Crossing": "detected", 
                    "Brake": "Hit",
                    "Camera ID": incoming_camera_id
                }
            else:
                helix_attributes = {
                    "Smart Relay": "On", 
                    "Line Crossing": "detected", 
                    "Brake": "FailtoBrake",
                    "Camera ID": incoming_camera_id
                }

        # Verkada API 호출 (Token 발급 -> Helix 전송)
        async with httpx.AsyncClient() as client:
            try:
                token_res = await client.post(
                    "https://api.verkada.com/token",
                    headers={"accept": "application/json", "content-type": "application/json", "x-api-key": VERKADA_API_KEY},
                    json={}
                )
                token_res.raise_for_status()
                session_token = token_res.json().get("token")
            except Exception as e:
                print(f"Token 발급 실패: {e}")
                return {"error": "Token request failed"}

            helix_payload = {
                "attributes": helix_attributes,
                "event_type_uid": "a600af14-5504-4b3a-a344-8fc514fdeded",
                "camera_id": TARGET_CAMERA_ID,
                "time_ms": int(time.time() * 1000)
            }
            
            try:
                helix_res = await client.post(
                    f"https://api.verkada.com/cameras/v1/video_tagging/event?org_id={ORG_ID}",
                    headers={"content-type": "application/json", "x-verkada-auth": session_token},
                    json=helix_payload
                )
                helix_res.raise_for_status()
                print("Helix API 전송 성공!")
                return {"status": "Helix Triggered"}
            except Exception as e:
                print(f"Helix 전송 실패: {e}")
                return {"error": "Helix request failed"}

    return {"message": "Unknown event ignored"}
