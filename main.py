from fastapi import FastAPI, Request
import asyncio
import httpx
import time
import os
import redis.asyncio as redis

app = FastAPI()

# 환경변수에서 설정값 불러오기
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
VERKADA_API_KEY = os.getenv("VERKADA_API_KEY")
TARGET_CAMERA_ID = os.getenv("TARGET_CAMERA_ID")
ORG_ID = os.getenv("ORG_ID", "607ef9ff-2910-4a68-bfe6-318836f42d12")

# Redis 클라이언트 연결 (URL 방식 사용)
redis_client = redis.from_url(REDIS_URL, decode_responses=True)

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

    if event_category == "heartbeat":
        await redis_client.set("last_heartbeat", int(time.time() * 1000))
        return {"message": "Heartbeat updated"}

    if event_category == "switch":
        await redis_client.set("switch_status", "1")
        return {"message": "Switch event recorded"}

    if event_category == "line_crossing":
        now = int(time.time() * 1000)
        last_heartbeat_str = await redis_client.get("last_heartbeat")
        last_heartbeat = int(last_heartbeat_str) if last_heartbeat_str else 0
        
        helix_attributes = {}

        if now - last_heartbeat > 10000:
            helix_attributes = {"Smart Relay": "Off", "Line Crossing": "detected", "Brake": "FailtoBrake"}
        else:
            await redis_client.set("switch_status", "0")
            await asyncio.sleep(5) 
            
            switch_status = await redis_client.get("switch_status")
            if switch_status == "1":
                helix_attributes = {"Smart Relay": "On", "Line Crossing": "detected", "Brake": "Hit"}
            else:
                helix_attributes = {"Smart Relay": "On", "Line Crossing": "detected", "Brake": "FailtoBrake"}

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
