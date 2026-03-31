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
        
        # 카메라 ID 추출 및 필터링
        incoming_camera_id = payload.get("data", {}).get("camera_id", "Unknown")
        if ALLOWED_CAMERAS and incoming_camera_id not in ALLOWED_CAMERAS:
            print(f"필터링됨: 허용되지 않은 카메라 (ID: {incoming_camera_id})")
            return {"message": "Camera ignored."}

        now = int(time.time() * 1000)
        
        # 쿨다운 로직 (중복 알림 방지)
        last_crossing_str = await redis_client.get("last_line_crossing")
        last_crossing = int(last_crossing_str) if last_crossing_str else 0
        if now - last_crossing < COOLDOWN_MS:
            return {"message": "Cooldown active."}
        await redis_client.set("last_line_crossing", now)

        # 하트비트 체크 (오프라인 판정)
        last_heartbeat_str = await redis_client.get("last_heartbeat")
        last_heartbeat = int(last_heartbeat_str) if last_heartbeat_str else 0
        
        # ★ [수정됨] 하트비트가 7초(7000ms) 이상 지연되면 오프라인으로 간주
        if now - last_heartbeat > 7000:
            print(f"오프라인 감지: 마지막 하트비트로부터 {now - last_heartbeat}ms 경과. Helix 전송 생략.")
            return {"message": "Shelly is offline (7s threshold). Helix skipped."}
            
        # 온라인 상태인 경우 5초 대기 후 브레이크 확인 로직 실행
        await redis_client.set("switch_status", "0")
        await asyncio.sleep(5) 
        
        switch_status = await redis_client.get("switch_status")
        
        helix_attributes = {
            "Smart Relay": "On", 
            "Line Crossing": "detected", 
            "Brake": "Hit" if switch_status == "1" else "FailtoBrake",
            "Camera ID": incoming_camera_id
        }

        # Verkada Helix API 전송
        async with httpx.AsyncClient() as client:
            try:
                # Token 발급
                token_res = await client.post(
                    "https://api.verkada.com/token",
                    headers={"accept": "application/json", "content-type": "application/json", "x-api-key": VERKADA_API_KEY},
                    json={}
                )
                token_res.raise_for_status()
                session_token = token_res.json().get("token")

                # Helix 이벤트 전송
                helix_payload = {
                    "attributes": helix_attributes,
                    "event_type_uid": "a600af14-5504-4b3a-a344-8fc514fdeded",
                    "camera_id": TARGET_CAMERA_ID,
                    "time_ms": int(time.time() * 1000)
                }
                
                helix_res = await client.post(
                    f"https://api.verkada.com/cameras/v1/video_tagging/event?org_id={ORG_ID}",
                    headers={"content-type": "application/json", "x-verkada-auth": session_token},
                    json=helix_payload
                )
                helix_res.raise_for_status()
                print(f"Helix API 전송 성공! (Brake: {helix_attributes['Brake']})")
                return {"status": "Helix Triggered"}
                
            except Exception as e:
                print(f"Verkada API 통신 에러: {e}")
                return {"error": str(e)}

    return {"message": "Unknown event ignored"}
