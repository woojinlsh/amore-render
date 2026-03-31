from fastapi import FastAPI, Request
import asyncio
import httpx
import time
import os
import redis.asyncio as redis

app = FastAPI()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
VERKADA_API_KEY = os.getenv("VERKADA_API_KEY")
TARGET_CAMERA_ID = os.getenv("TARGET_CAMERA_ID")
ORG_ID = os.getenv("ORG_ID", "607ef9ff-2910-4a68-bfe6-318836f42d12")

redis_client = redis.from_url(REDIS_URL, decode_responses=True)

# 쿨다운 타임 설정 (예: 15초 = 15000ms)
# 한 번 Line Crossing이 발생하면 15초 동안 들어오는 추가 알림은 무시합니다.
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

    # 3. Line Crossing 처리 (쿨다운 적용)
    if event_category == "line_crossing":
        now = int(time.time() * 1000)
        
        # --- [쿨다운 로직 시작] ---
        last_crossing_str = await redis_client.get("last_line_crossing")
        last_crossing = int(last_crossing_str) if last_crossing_str else 0
        
        # 마지막으로 처리한 시간으로부터 COOLDOWN_MS(15초)가 지나지 않았다면 무시
        if now - last_crossing < COOLDOWN_MS:
            print("쿨다운 적용 중: 중복 Line Crossing 이벤트 무시됨")
            return {"message": "Cooldown active. Ignored."}
            
        # 쿨다운 통과 시, 현재 시간을 마지막 처리 시간으로 갱신
        await redis_client.set("last_line_crossing", now)
        # --- [쿨다운 로직 끝] ---

        last_heartbeat_str = await redis_client.get("last_heartbeat")
        last_heartbeat = int(last_heartbeat_str) if last_heartbeat_str else 0
        
        helix_attributes = {}

        # 조건 A: 지게차 시동 꺼짐 (10초 이상 Heartbeat 없음)
        if now - last_heartbeat > 10000:
            helix_attributes = {"Smart Relay": "Off", "Line Crossing": "detected", "Brake": "FailtoBrake"}
        else:
            # 조건 B: 지게차 시동 켜짐 (5초간 브레이크 대기)
            await redis_client.set("switch_status", "0")
            await asyncio.sleep(5) 
            
            switch_status = await redis_client.get("switch_status")
            if switch_status == "1":
                helix_attributes = {"Smart Relay": "On", "Line Crossing": "detected", "Brake": "Hit"}
            else:
                helix_attributes = {"Smart Relay": "On", "Line Crossing": "detected", "Brake": "FailtoBrake"}

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
