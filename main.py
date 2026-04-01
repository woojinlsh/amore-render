from fastapi import FastAPI, Request
import asyncio
import httpx
import time
import os
import json
import redis.asyncio as redis

app = FastAPI()

# 환경변수 설정
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
VERKADA_API_KEY = os.getenv("VERKADA_API_KEY")
TARGET_CAMERA_ID = os.getenv("TARGET_CAMERA_ID")
ORG_ID = os.getenv("ORG_ID", "607ef9ff-2910-4a68-bfe6-318836f42d12")
HELIX_EVENT_TYPE_UID = os.getenv("HELIX_EVENT_TYPE_UID", "a600af14-5504-4b3a-a344-8fc514fdeded")

ALLOWED_CAMERAS_STR = os.getenv("ALLOWED_CAMERA_IDS", "")
ALLOWED_CAMERAS = [cam.strip() for cam in ALLOWED_CAMERAS_STR.split(",") if cam.strip()]

redis_client = redis.from_url(REDIS_URL, decode_responses=True)
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
        incoming_camera_id = payload.get("data", {}).get("camera_id", "Unknown")
        if ALLOWED_CAMERAS and incoming_camera_id not in ALLOWED_CAMERAS:
            return {"message": "Camera ignored."}

        now = int(time.time() * 1000)
        
        # 쿨다운 로직
        last_crossing_str = await redis_client.get("last_line_crossing")
        last_crossing = int(last_crossing_str) if last_crossing_str else 0
        if now - last_crossing < COOLDOWN_MS:
            return {"message": "Cooldown active."}
        await redis_client.set("last_line_crossing", now)

        # 하트비트 체크
        last_heartbeat_str = await redis_client.get("last_heartbeat")
        last_heartbeat = int(last_heartbeat_str) if last_heartbeat_str else 0
        if now - last_heartbeat > 7000:
            print("Shelly 오프라인 상태로 판단하여 무시합니다.")
            return {"message": "Shelly is offline."}
            
        # 5초 실시간 감지 루프
        await redis_client.set("switch_status", "0")
        
        brake_detected = False
        start_time = time.time()
        
        print(f"[{incoming_camera_id}] Line Crossing 발생! 5초간 브레이크 감지 시작...")
        
        while time.time() - start_time < 5.0:
            current_status = await redis_client.get("switch_status")
            if current_status == "1":
                brake_detected = True
                print(f"[{incoming_camera_id}] 5초 이내 브레이크 감지 성공!")
                break
            await asyncio.sleep(0.2)
            
        if not brake_detected:
            print(f"[{incoming_camera_id}] 5초 이내 브레이크 감지 실패.")

        helix_attributes = {
            "Smart Relay": "On", 
            "Line Crossing": "detected", 
            "Brake": "Hit" if brake_detected else "FailtoBrake",
            "Camera ID": incoming_camera_id
        }

        # Verkada Helix API 전송
        async with httpx.AsyncClient() as client:
            try:
                token_res = await client.post(
                    "https://api.verkada.com/token",
                    headers={"accept": "application/json", "content-type": "application/json", "x-api-key": VERKADA_API_KEY},
                    json={}
                )
                token_res.raise_for_status()
                session_token = token_res.json().get("token")

                helix_payload = {
                    "attributes": helix_attributes,
                    "event_type_uid": HELIX_EVENT_TYPE_UID, 
                    "camera_id": TARGET_CAMERA_ID,
                    "time_ms": int(time.time() * 1000)
                }
                
                # ★ [디버그 1] Verkada로 쏘기 직전의 원본 데이터를 로그에 이쁘게 찍어봅니다.
                print(f"\n========== [DEBUG: 보내는 데이터] ==========")
                print(json.dumps(helix_payload, indent=2, ensure_ascii=False))
                print(f"============================================")
                
                helix_res = await client.post(
                    f"https://api.verkada.com/cameras/v1/video_tagging/event?org_id={ORG_ID}",
                    headers={"content-type": "application/json", "x-verkada-auth": session_token},
                    json=helix_payload
                )
                
                # ★ [디버그 2] Verkada 서버의 실제 응답 내용을 찍어봅니다.
                print(f"\n========== [DEBUG: Verkada 응답] ===========")
                print(f"상태 코드: {helix_res.status_code}")
                print(f"응답 내용: {helix_res.text}")
                print(f"============================================\n")
                
                helix_res.raise_for_status()
                print(f"Helix 전송 성공! 결과: {helix_attributes['Brake']}")
                return {"status": "Helix Triggered", "result": helix_attributes['Brake']}
                
            except Exception as e:
                print(f"API 에러: {e}")
                return {"error": str(e)}

    return {"message": "Unknown event ignored"}
