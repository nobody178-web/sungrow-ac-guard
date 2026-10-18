import os
import sys
sys.stdout.reconfigure(line_buffering=True)
import time
import base64
import requests
from datetime import datetime, timezone, timedelta

# ======================== TIME CONFIGURATION & GATE ========================
PKT_TZ = timezone(timedelta(hours=5))  # Pakistan Standard Time (UTC+5)

# 2:00 PM = 14:00 (840 mins), 4:30 PM = 16:30 (990 mins)
START_MINUTES = 840
END_MINUTES = 990

now_initial = datetime.now(PKT_TZ)
current_minute = now_initial.hour * 60 + now_initial.minute

if not (START_MINUTES <= current_minute <= END_MINUTES):
    print(f"[{now_initial.strftime('%H:%M:%S')} PKT] Outside active window (14:00 - 16:30 PKT).")
    print("[*] Exiting cleanly without querying Sungrow or SmartThings.")
    sys.exit(0)

# ======================== CONFIGURATION & SECRETS ========================
SUNGROW_APP_KEY = os.environ.get("SUNGROW_APP_KEY")
SUNGROW_APP_SECRET = os.environ.get("SUNGROW_APP_SECRET")
SUNGROW_USERNAME = os.environ.get("SUNGROW_USERNAME")
SUNGROW_PASSWORD = os.environ.get("SUNGROW_PASSWORD")
PS_KEY = os.environ.get("SUNGROW_PS_KEY")

ST_CLIENT_ID = os.environ.get("SMARTTHINGS_CLIENT_ID")
ST_CLIENT_SECRET = os.environ.get("SMARTTHINGS_CLIENT_SECRET")
ST_REFRESH_TOKEN = os.environ.get("SMARTTHINGS_REFRESH_TOKEN")
AC_DEVICE_ID = os.environ.get("SMARTTHINGS_DEVICE_ID")

SUNGROW_BASE_URL = "https://openapi.isolarcloud.com.hk"

INTERVAL_SEC = 360    # 6 minutes per poll
CYCLES_PER_HOUR = 10  # 10 cycles * 6 min = 60 minutes

required = [
    SUNGROW_APP_KEY, SUNGROW_APP_SECRET, SUNGROW_USERNAME, SUNGROW_PASSWORD,
    PS_KEY, ST_CLIENT_ID, ST_CLIENT_SECRET, ST_REFRESH_TOKEN, AC_DEVICE_ID
]
if any(not item for item in required):
    print("[-] Error: Missing required environment secrets. Terminating.")
    sys.exit(1)

sungrow_headers = {
    "Content-Type": "application/json;charset=UTF-8",
    "sys_code": "901",
    "x-access-key": SUNGROW_APP_SECRET
}

# ======================== SMARTTHINGS ADAPTER ========================
st_access_token = None

def refresh_smartthings_token():
    """Generates a fresh 24h SmartThings access token using OAuth refresh token."""
    global st_access_token
    url = "https://api.smartthings.com/oauth/token"
    auth_header = base64.b64encode(f"{ST_CLIENT_ID}:{ST_CLIENT_SECRET}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_header}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {
        "grant_type": "refresh_token",
        "client_id": ST_CLIENT_ID,
        "refresh_token": ST_REFRESH_TOKEN
    }
    for attempt in range(1, 4):
        try:
            res = requests.post(url, headers=headers, data=data, timeout=15)
            if res.status_code == 200:
                st_access_token = res.json().get("access_token")
                print(f"[+] SmartThings token refreshed successfully (Attempt {attempt}).")
                return True
            print(f"[!] SmartThings refresh rejected (Code {res.status_code}): {res.text}")
        except Exception as e:
            print(f"[!] SmartThings refresh connection error: {e}")
        time.sleep(3)
    return False

def get_ac_state():
    """Reads current AC operating state safely."""
    global st_access_token
    if not st_access_token and not refresh_smartthings_token():
        return None

    url = f"https://api.smartthings.com/v1/devices/{AC_DEVICE_ID}/status"
    headers = {"Authorization": f"Bearer {st_access_token}", "Content-Type": "application/json"}
    
    for attempt in range(1, 4):
        try:
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code == 401:
                print("[!] SmartThings token expired during status check. Refreshing...")
                if refresh_smartthings_token():
                    headers["Authorization"] = f"Bearer {st_access_token}"
                    continue
            if r.status_code == 200:
                main_comp = r.json().get("components", {}).get("main", {})
                power = main_comp.get("switch", {}).get("switch", {}).get("value", "off")
                mode = main_comp.get("airConditionerMode", {}).get("airConditionerMode", {}).get("value", "unknown")
                setpoint = main_comp.get("thermostatCoolingSetpoint", {}).get("coolingSetpoint", {}).get("value", 0)
                fan = main_comp.get("airConditionerFanMode", {}).get("fanMode", {}).get("value", "auto")
                return {"power": power, "mode": mode, "setpoint": setpoint, "fanMode": fan}
            print(f"[!] SmartThings status error (Code {r.status_code}): {r.text}")
        except Exception as e:
            print(f"[!] Network error checking AC status ({attempt}/3): {e}")
        time.sleep(3)
    return None

def execute_st_commands(commands):
    """Dispatches commands with retry and token re-auth support."""
    global st_access_token
    if not st_access_token and not refresh_smartthings_token():
        return False, "Failed to authenticate with SmartThings"

    url = f"https://api.smartthings.com/v1/devices/{AC_DEVICE_ID}/commands"
    payload = {"commands": commands}

    for attempt in range(1, 4):
        headers = {"Authorization": f"Bearer {st_access_token}", "Content-Type": "application/json"}
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=15)
            if r.status_code == 401:
                print("[!] SmartThings token expired during command execution. Refreshing...")
                if refresh_smartthings_token():
                    continue
            if r.status_code == 200:
                return True, "Executed successfully"
            print(f"[!] Command execution rejected (Code {r.status_code}): {r.text}")
        except Exception as e:
            print(f"[!] Network failure dispatching commands ({attempt}/3): {e}")
        time.sleep(3)
    return False, "Failed to send commands after 3 attempts"

def set_ac_off(current_state):
    if current_state and current_state.get("power") == "off":
        return True, "Already OFF (Skipping command)"
    commands = [{"component": "main", "capability": "switch", "command": "off"}]
    return execute_st_commands(commands)

def set_ac_eco(current_state):
    commands = []
    if not current_state or current_state.get("power") != "on":
        commands.append({"component": "main", "capability": "switch", "command": "on"})
    if not current_state or current_state.get("mode") != "cool":
        commands.append({"component": "main", "capability": "airConditionerMode", "command": "setAirConditionerMode", "arguments": ["cool"]})
    commands.append({"component": "main", "capability": "custom.airConditionerOptionalMode", "command": "setAcOptionalMode", "arguments": ["off"]})
    if not current_state or current_state.get("setpoint") != 26:
        commands.append({"component": "main", "capability": "thermostatCoolingSetpoint", "command": "setCoolingSetpoint", "arguments": [26]})
    if not current_state or current_state.get("fanMode") != "low":
        commands.append({"component": "main", "capability": "airConditionerFanMode", "command": "setFanMode", "arguments": ["low"]})

    if not commands:
        return True, "Already in ECO configuration (Cool @ 26°C, Low Fan)"
    return execute_st_commands(commands)

# ======================== SUNGROW ADAPTER ========================
sungrow_token = None

def login_sungrow():
    """Logs into Sungrow OpenAPI with error recovery."""
    global sungrow_token
    payload = {
        "appkey": SUNGROW_APP_KEY,
        "user_account": SUNGROW_USERNAME,
        "user_password": SUNGROW_PASSWORD
    }
    for attempt in range(1, 4):
        try:
            r = requests.post(f"{SUNGROW_BASE_URL}/openapi/login", json=payload, headers=sungrow_headers, timeout=15)
            res = r.json()
            if res.get("result_code") == "1":
                sungrow_token = res.get("result_data", {}).get("token")
                print(f"[+] Sungrow login established on attempt {attempt}.")
                return True
            print(f"[!] Sungrow login failed: {res.get('result_msg')}")
        except Exception as e:
            print(f"[!] Sungrow network issue on login ({attempt}/3): {e}")
        time.sleep(4)
    return False

def get_telemetry():
    """Reads live telemetry registers with session renewal."""
    global sungrow_token
    if not sungrow_token and not login_sungrow():
        return None

    payload = {
        "appkey": SUNGROW_APP_KEY,
        "token": sungrow_token,
        "device_type": 14,
        "point_id_list": ["13170", "13141", "13003", "13150", "13119"],
        "ps_key_list": [PS_KEY]
    }

    for attempt in range(1, 4):
        try:
            r = requests.post(f"{SUNGROW_BASE_URL}/openapi/getDeviceRealTimeData", json=payload, headers=sungrow_headers, timeout=15)
            res = r.json()
            if res.get("result_code") in ["102", "103", "201"]:
                print("[!] Sungrow token expired. Re-authenticating...")
                if login_sungrow():
                    payload["token"] = sungrow_token
                    continue
            if res.get("result_code") == "1":
                return res
            print(f"[!] Sungrow telemetry response error: {res.get('result_msg')}")
        except Exception as e:
            print(f"[!] Sungrow connection failure ({attempt}/3): {e}")
        time.sleep(3)
    return None

# ======================== MAIN EXECUTION ========================
print("[*] Initializing Sungrow AC Guard...")
refresh_smartthings_token()
login_sungrow()

for cycle in range(1, CYCLES_PER_HOUR + 1):
    now_pkt = datetime.now(PKT_TZ)
    time_minutes = now_pkt.hour * 60 + now_pkt.minute
    now_str = now_pkt.strftime("%H:%M:%S")

    # If the time exceeds the 4:30 PM cutoff during a 1-hour block
    if time_minutes > END_MINUTES:
        print(f"[{now_str} PKT] Active afternoon window concluded (> 16:30 PKT). Ensuring AC is OFF and stopping.")
        ac_state = get_ac_state()
        set_ac_off(ac_state)
        break

    data_res = get_telemetry()
    ac_state = get_ac_state()

    if not data_res:
        print(f"[{now_str} PKT] Telemetry unreachable. Skipping cycle without disrupting AC.")
    else:
        pts = {}
        pt_list = data_res.get("result_data", {}).get("device_point_list", [])
        if pt_list:
            for k, v in pt_list[0].get("device_point", {}).items():
                if k.startswith("p") and v is not None:
                    pts[k[1:]] = float(v)

        raw_bms_soc = pts.get("13170", 0.0)
        raw_inv_soc = pts.get("13141", 0.0)
        soc = raw_bms_soc if raw_bms_soc > 0.0 else raw_inv_soc
        if 0.0 < soc <= 1.0:
            soc *= 100.0

        pv_w = pts.get("13003", 0.0)
        discharge_w = pts.get("13150", 0.0)
        load_w = pts.get("13119", 0.0)

        ac_status_str = f"{ac_state['power'].upper()} ({ac_state['mode']} @ {ac_state['setpoint']}°C, {ac_state['fanMode']})" if ac_state else "UNKNOWN"

        print(f"[{now_str} PKT] Cycle {cycle}/{CYCLES_PER_HOUR} | "
              f"SOC: {soc:.1f}% | PV: {pv_w:.0f}W | Load: {load_w:.0f}W | Discharge: {discharge_w:.0f}W | "
              f"AC: {ac_status_str}")

        # ======================= DECISION ENGINE =======================
        target_action = "LEAVE_AS_IS"
        reason = ""

        # Condition 1: Evening Cutoff (After 4:30 PM [990 mins] or PV < 300W)
        if time_minutes >= 990 or pv_w < 300.0:
            target_action = "SET_OFF"
            reason = f"Evening/Sunset cutoff (Time >= 16:30 or PV < 300W [Current: {pv_w:.0f}W])"

        # Condition 2: Window 3:00 PM to 4:30 PM (900 <= time < 990 mins)
        elif 900 <= time_minutes < 990:
            if soc < 95.0:
                target_action = "SET_OFF"
                reason = f"SOC {soc:.1f}% is < 95% after 3 PM. Off to prioritize battery charge."
            else:
                if pv_w >= load_w and discharge_w <= 100.0:
                    target_action = "SET_ECO"
                    reason = f"SOC >= 95% with Solar Surplus (PV {pv_w:.0f}W >= Load {load_w:.0f}W). ECO enabled."
                else:
                    target_action = "SET_OFF"
                    reason = f"SOC >= 95% but Solar Deficit (Discharge {discharge_w:.0f}W > 100W or PV < Load). Turning OFF."

        # Condition 3: Window 2:00 PM to 3:00 PM (840 <= time < 900 mins)
        elif 840 <= time_minutes < 900:
            if soc < 90.0 and discharge_w > 500.0:
                target_action = "SET_ECO"
                reason = f"SOC {soc:.1f}% < 90% AND Discharge {discharge_w:.0f}W > 500W. Throttling to ECO (26°C)."
            else:
                target_action = "LEAVE_AS_IS"
                reason = "Safe operating range (SOC >= 90% or Discharge <= 500W). Leaving AC untouched."

        # Condition 4: Before 2:00 PM (time < 840 mins)
        else:
            target_action = "LEAVE_AS_IS"
            reason = "Before 2:00 PM generation window. Leaving AC untouched."

        # ======================= DISPATCHER =======================
        print(f" -> Decision: {target_action} | Reason: {reason}")

        if target_action == "SET_OFF":
            ok, msg = set_ac_off(ac_state)
            print(f"    [Action OFF] {msg}")
        elif target_action == "SET_ECO":
            ok, msg = set_ac_eco(ac_state)
            print(f"    [Action ECO] {msg}")
        elif target_action == "LEAVE_AS_IS":
            print("    [Action] No command sent. Current AC state preserved.")

    if cycle < CYCLES_PER_HOUR:
        print(f" -> Next sample in 6 minutes (360s)...\n")
        time.sleep(INTERVAL_SEC)

print("[+] Completed session cleanly.")
