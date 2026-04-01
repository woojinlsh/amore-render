from fastapi import FastAPI, Request
import asyncio
import httpx
import time
import os
import json
import redis.asyncio as redis

app = FastAPI()

# --- [설정값(Config) 불러오기 영역] ---
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
VERKADA_API_KEY = os.getenv("VERKADA_API_KEY")
TARGET_CAMERA_ID = os.getenv("TARGET_CAMERA_ID")
ORG_ID = os.getenv("ORG_ID", "607ef9ff-2910-4a68-bfe6-318836f42d12")
HELIX_EVENT_TYPE_UID = os.getenv("HELIX_EVENT_TYPE_UID", "a600af14-5504-4b3a-a344-8fc514fdeded")

# 허용할 카메라 ID 목록
ALLOWED_CAMERAS_STR = os.getenv("ALLOWED_CAMERA_IDS", "")
ALLOWED_CAMERAS = [cam.strip() for cam in ALLOWED_CAMERAS_STR.split(",") if cam.strip()]
# -------------------------------------

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
        
        # 쿨다운 로직 (15초 이내 중복 방지)
        last_crossing_str = await redis_client.get("last_line_crossing")
        last_crossing = int(last_crossing_str) if last_crossing_str else 0
        if now - last_crossing < COOLDOWN_MS:
            return {"message": "Cooldown active."}
        await redis_client.set("last_line_crossing", now)

        # 하트비트 체크 (7초 이상 소식이 없으면 오프라인 판정)
        last_heartbeat_str = await redis_client.get("last_heartbeat")
        last_heartbeat = int(last_heartbeat_str) if last_heartbeat_str else 0
        if now - last_heartbeat > 7000:
            print(f"[{incoming_camera_id}] Shelly 오프라인 상태(7초 초과)로 판단하여 무시합니다.")
            return {"message": "Shelly is offline."}
            
        # --- [5초 실시간 감지 루프 시작] ---
        await redis_client.set("switch_status", "0") # 스위치 상태 초기화
        
        brake_detected = False
        start_time = time.time()
        
        print(f"\n[{incoming_camera_id}] Line Crossing 발생! 5초간 브레이크 감지 시작...")
        
        while time.time() - start_time < 5.0:
            current_status = await redis_client.get("switch_status")
            if current_status == "1":
                brake_detected = True
                # ★ 브레이크 HIT 감지 시 눈에 확 띄는 로그
                print(f"\n" + "="*50)
                print(f" 🚨🚨🚨 [BRAKE: HIT] 브레이크 정상 작동 감지! 🚨🚨🚨")
                print(f"        (Camera ID: {incoming_camera_id})")
                print("="*50 + "\n")
                break
            await asyncio.sleep(0.2) # 서버 부하를 줄이기 위한 미세 대기
            
        if not brake_detected:
            # ★ 브레이크 실패 시 눈에 띄는 로그
            print(f"\n" + "-"*50)
            print(f" ❌❌❌ [BRAKE: FAIL] 5초 내 브레이크 감지 실패 ❌❌❌")
            print(f"        (Camera ID: {incoming_camera_id})")
            print("-"*50 + "\n")
        # -----------------------------------

        helix_attributes = {
            "Smart Relay": "On", 
            "Line Crossing": "detected", 
            "Brake": "Hit" if brake_detected else "FailtoBrake",
            "Camera ID": incoming_camera_id
        }

        # Verkada Helix API 전송
        async with httpx.AsyncClient() as client:
            try:
                # [STEP 1] Token 발급
                token_res = await client.post(
                    "https://api.verkada.com/token",
                    headers={"accept": "application/json", "content-type": "application/json", "x-api-key": VERKADA_API_KEY},
                    json={}
                )
                token_res.raise_for_status()
                session_token = token_res.json().get("token")

                # [STEP 2] Helix 이벤트 전송
                helix_payload = {
                    "attributes": helix_attributes,
                    "event_type_uid": HELIX_EVENT_TYPE_UID, 
                    "camera_id": TARGET_CAMERA_ID,
                    "time_ms": int(time.time() * 1000)
                }
                
                # (선택) Verkada로 쏘기 직전의 원본 데이터 디버그 출력
                print(f"========== [DEBUG: 보내는 데이터] ==========")
                print(json.dumps(helix_payload, indent=2, ensure_ascii=False))
                print(f"============================================")
                
                helix_res = await client.post(
                    f"https://api.verkada.com/cameras/v1/video_tagging/event?org_id={ORG_ID}",
                    headers={"content-type": "application/json", "x-verkada-auth": session_token},
                    json=helix_payload
                )
                
                helix_res.raise_for_status()
                
                # ★ Helix 전송 최종 결과 구분 로그
                if brake_detected:
                    print(f"✅ [SUCCESS] Helix 전송 완료 -> 상태: [Hit] (정상 정지)\n")
                else:
                    print(f"⚠️ [WARNING] Helix 전송 완료 -> 상태: [FailtoBrake] (위험)\n")
                    
                return {"status": "Helix Triggered", "result": helix_attributes['Brake']}
                
            except Exception as e:
                print(f"API 에러: {e}")
                return {"error": str(e)}

    return {"message": "Unknown event ignored"}
