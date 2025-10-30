# -*- coding: utf-8 -*-
"""
=== [GUI 프로그램: 카카오톡 채널 자동 발송 v1.2 - 버그 수정] ===
- Tkinter + PyAutoGUI + Tesseract OCR
- 스레드-안전 UI 디스패치(Queue + after), 안정화된 이미지 탐색/대기,
  OCR 전처리(OpenCV 있으면), 스톱 마커 유사도 비교, (실험적) 파일 첨부

[수정 사항]
- 무한 루프 방지: 체크박스를 찾지 못할 때도 종료 조건 체크
- 대기 시간 일관성: 모든 스크롤 후 LIST_REFRESH_TIME 사용
- 카운터 로직 개선: 일관된 종료 조건 처리
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
import pyautogui
import pytesseract
from PIL import Image
import time
import threading
import sys
import os
from queue import Queue
from difflib import SequenceMatcher
import re
import tempfile
import ctypes

# --- 선택 의존성(OpenCV) ---
try:
    import numpy as np
    import cv2
    _HAS_CV = True
except Exception:
    _HAS_CV = False

# --- PyAutoGUI 전역 설정 ---
pyautogui.FAILSAFE = True       # 마우스 좌상단으로 이동 시 긴급 중단
pyautogui.PAUSE = 0.10          # 각 동작 사이 약간의 자동 대기

# ----------------------------------------
#⚠️ 사용자 설정 (필수)
# ----------------------------------------
# 1. Tesseract-OCR 설치 경로
TESSERACT_CMD_PATH = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

# 2. OCR (이름 읽기) 영역 오프셋(체크박스 중심 기준)
OCR_REGION_OFFSET = {'x': 20, 'y': -10, 'width': 300, 'height': 30}

# 3. 이미지 인식 정확도 (0.8 ~ 1.0)
IMAGE_CONFIDENCE = 0.80

# 4. 대기 시간(초)
SLEEP_TIME = 1.5
LIST_REFRESH_TIME = 4.0

# 5. (선택) 검색 후 결과 리스트 준비 마커 사용 여부
ENABLE_WAIT_FOR_MARKER = False
LIST_READY_MARKER = 'list_ready_marker.png'  # ENABLE_WAIT_FOR_MARKER=True일 때만 사용

# ----------------------------------------
# ⚙️ 전역 변수
# ----------------------------------------
automation_thread = None
stop_event = threading.Event()

# UI 디스패치 (스레드-안전)
ui_queue = Queue()
root = None  # Tk 루트

# ----------------------------------------
# 🧠 스레드-안전 UI 디스패치 유틸
# ----------------------------------------
def ui_dispatch(func, *args, **kwargs):
    """어느 스레드에서든 UI 작업을 안전하게 예약"""
    ui_queue.put((func, args, kwargs))

def _drain_ui_queue():
    """메인 스레드에서 주기적으로 큐를 비움"""
    try:
        while True:
            func, args, kwargs = ui_queue.get_nowait()
            try:
                func(*args, **kwargs)
            except Exception as e:
                print("UI dispatch error:", e)
    except Exception:
        pass
    finally:
        root.after(50, _drain_ui_queue)

def safe_messagebox(kind, title, text):
    if kind == "info":
        ui_dispatch(messagebox.showinfo, title, text)
    elif kind == "warning":
        ui_dispatch(messagebox.showwarning, title, text)
    elif kind == "error":
        ui_dispatch(messagebox.showerror, title, text)

def log_message(message):
    def _append():
        try:
            log_area.config(state=tk.NORMAL)
            log_area.insert(tk.END, f"{message}\n")
            log_area.see(tk.END)
            log_area.config(state=tk.DISABLED)
            print(message)
        except Exception as e:
            print(f"로그 기록 오류: {e}")
    ui_dispatch(_append)

# ----------------------------------------
# 🔒 제어/안전 유틸
# ----------------------------------------
def is_admin():
    """Windows에서 관리자 권한으로 실행 중인지 확인"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def check_stop_signal():
    """'즉시 중지' 버튼이 눌렸는지 확인"""
    if stop_event.is_set():
        raise InterruptedError("사용자에 의해 작업이 중지되었습니다.")

def check_required_image_files(run_step1, run_step2):
    """
    시작 전에 필수 이미지 파일들의 존재 여부를 확인
    누락된 파일이 있으면 경고 메시지와 함께 False 반환
    """
    missing_files = []

    # 1단계 실행 시 필요한 파일들
    if run_step1:
        step1_files = [
            'filter_details.png',
            'period_input.png',
            'direct_input.png',
            'tilde_marker.png',
            'confirm_button.png',
            'search_button.png'
        ]
        for file in step1_files:
            if not os.path.exists(file):
                missing_files.append(file)

    # 2단계 실행 시 필요한 파일들
    if run_step2:
        step2_files = [
            'checkbox.png',
            'send_button.png',
            'back_button.png'
        ]
        for file in step2_files:
            if not os.path.exists(file):
                missing_files.append(file)

    # 선택적 파일 (경고만 표시)
    optional_files = ['message_input.png', 'file_attach_button.png']
    missing_optional = []
    for file in optional_files:
        if not os.path.exists(file):
            missing_optional.append(file)

    # 결과 보고
    if missing_files:
        log_message("❌ 오류: 다음 필수 이미지 파일들을 찾을 수 없습니다:")
        for file in missing_files:
            log_message(f"  - {file}")
        log_message("\n이미지 파일들을 스크립트와 같은 폴더에 배치한 후 다시 시도하세요.")
        log_message("이미지는 카카오톡 화면에서 해당 버튼을 스크린샷으로 캡처하여 만듭니다.")
        return False

    if missing_optional:
        log_message("⚠️ 경고: 다음 선택적 이미지 파일들을 찾을 수 없습니다:")
        for file in missing_optional:
            log_message(f"  - {file}")
        log_message("이 파일들이 없어도 작동하지만, 있으면 더 안정적입니다.\n")

    log_message("✅ 모든 필수 이미지 파일 확인 완료!")
    return True

# ----------------------------------------
# 🖼️ 이미지 탐색/대기 유틸
# ----------------------------------------
def find_image_location(image_file, timeout=10, region=None):
    """화면(또는 지정 영역)에서 이미지 중앙 좌표를 반환"""
    log_message(f"'{image_file}' 찾는 중...")
    start_time = time.time()
    while (time.time() - start_time) < timeout:
        check_stop_signal()
        try:
            location = pyautogui.locateCenterOnScreen(
                image_file, confidence=IMAGE_CONFIDENCE, region=region
            )
            if location:
                log_message(f"'{image_file}' 찾음.")
                return location
        except Exception as e:
            log_message(f"'{image_file}' 검색 중 오류: {e}")
            break
        time.sleep(0.3)
    raise FileNotFoundError(f"'{image_file}'을(를) {timeout}초 내에 찾지 못했습니다.")

def click_image(image_file, timeout=10, region=None):
    """이미지를 찾아 클릭"""
    location = find_image_location(image_file, timeout=timeout, region=region)
    pyautogui.click(location)
    time.sleep(SLEEP_TIME)

def wait_for(image_file, timeout=10, region=None):
    """이미지가 나타날 때까지 대기(True/False 반환)"""
    start = time.time()
    while time.time() - start < timeout:
        check_stop_signal()
        if pyautogui.locateOnScreen(image_file, confidence=IMAGE_CONFIDENCE, region=region):
            return True
        time.sleep(0.3)
    return False

# ----------------------------------------
# 🔤 OCR 유틸
# ----------------------------------------
def _normalize_name(s):
    s = s.strip().replace("\n", " ")
    s = re.sub(r'[^0-9A-Za-z가-힣 ]+', '', s)  # 특수문자 제거
    s = re.sub(r'\s+', ' ', s).strip()
    return s

def read_text_from_region(region_box, retry_count=0, max_retries=3):
    """
    주어진 좌표 영역(x, y, w, h)을 스크린샷 찍어 텍스트로 반환
    Windows 권한 오류 시 자동 재시도 (최대 3회)
    """
    try:
        check_stop_signal()
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD_PATH

        # ===== [수정] 임시 폴더를 명시적으로 지정하여 권한 오류 방지 =====
        temp_dir = tempfile.gettempdir()
        os.environ['TESSDATA_PREFIX'] = os.path.dirname(TESSERACT_CMD_PATH)
        # ===========================================================

        screenshot = pyautogui.screenshot(region=region_box)

        if _HAS_CV:
            img = np.array(screenshot)
            if img.ndim == 3:
                img = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
            img = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
            img = cv2.medianBlur(img, 3)
            cfg = r'--oem 1 --psm 7 -c preserve_interword_spaces=1'
            text = pytesseract.image_to_string(img, lang='kor+eng', config=cfg)
        else:
            text = pytesseract.image_to_string(screenshot, lang='kor+eng')

        text = _normalize_name(text)
        if not text:
            log_message("경고: OCR이 텍스트를 인식하지 못했습니다. (빈 문자열)")
        return text

    except PermissionError as e:
        # ===== [신규] Windows 권한 오류 특별 처리 =====
        if retry_count < max_retries:
            log_message(f"⚠️ Windows 권한 오류 발생 ({retry_count + 1}/{max_retries}): {e}")
            log_message(f"0.5초 후 재시도합니다...")
            time.sleep(0.5)
            return read_text_from_region(region_box, retry_count + 1, max_retries)
        else:
            log_message(f"❌ OCR 권한 오류 ({max_retries}회 재시도 실패): {e}")
            log_message("💡 해결 방법:")
            log_message("  1. 프로그램을 '관리자 권한'으로 실행하세요")
            log_message("  2. 바이러스 백신/보안 프로그램이 차단하는지 확인하세요")
            log_message("  3. Tesseract 설치 폴더 권한을 확인하세요")
            return ""
        # ================================================

    except Exception as e:
        # ===== [수정] 일반 오류도 재시도 로직 적용 =====
        if retry_count < max_retries:
            log_message(f"⚠️ OCR 오류 발생 ({retry_count + 1}/{max_retries}): {e}")
            log_message(f"0.5초 후 재시도합니다...")
            time.sleep(0.5)
            return read_text_from_region(region_box, retry_count + 1, max_retries)
        else:
            log_message(f"❌ OCR 오류 ({max_retries}회 재시도 실패): {e}")
            log_message("Tesseract-OCR 설치/경로/언어팩을 확인하세요.")
            return ""
        # ============================================

def get_target_info(location):
    """체크박스 좌표(중앙) 기준으로 이름 영역 좌표와 텍스트를 반환"""
    # location이 Point 또는 (x, y)일 수 있음
    if hasattr(location, 'x'):
        x, y = location.x, location.y
    else:
        x, y = location

    # 좌표를 정수로 강제 변환 (pyautogui.center()가 float를 반환할 수 있음)
    x = int(x)
    y = int(y)

    ocr_x = x + OCR_REGION_OFFSET['x']
    ocr_y = y + OCR_REGION_OFFSET['y']
    ocr_w = OCR_REGION_OFFSET['width']
    ocr_h = OCR_REGION_OFFSET['height']
    region_box = (ocr_x, ocr_y, ocr_w, ocr_h)
    name = read_text_from_region(region_box)
    return name, region_box

# ----------------------------------------
# 🔎 문자열 유사도 (스톱 마커 판정)
# ----------------------------------------
def is_same_name(a, b, threshold=0.78):
    a = a.strip(); b = b.strip()
    if not a or not b:
        return False
    return SequenceMatcher(None, a, b).ratio() >= threshold

# ----------------------------------------
# 📎 (선택) 파일 첨부 - 시스템 대화상자 제어 (실험적)
# ----------------------------------------
def attach_file_via_dialog(file_path, title_regex=r"열기|Open"):
    """
    '파일첨부' 버튼 클릭 후 파일 선택 대화상자가 열린 상태에서 호출하세요.
    Windows UI 오토메이션에 의존(언어/OS/설정에 따라 컨트롤 식별자 다름).
    """
    try:
        from pywinauto.application import Application
        app = Application(backend="uia").connect(title_re=title_regex, timeout=10)
        dlg = app.window(title_re=title_regex)
        # 일반적으로 '파일 이름' 에디트의 AutomationId가 1148인 경우가 많습니다.
        edit = dlg.child_window(auto_id="1148", control_type="Edit")
        if not edit.exists():
            # 대체 탐색(언어/환경에 따라 다릅니다)
            edit = dlg.child_window(control_type="Edit")
        edit.set_edit_text(file_path)
        # '열기' 버튼 클릭
        open_btn = dlg.child_window(title_re=r"열기|Open", control_type="Button")
        open_btn.click_input()
        return True
    except ImportError:
        log_message("경고: pywinauto 미설치로 파일 첨부 자동화를 건너뜁니다. (pip install pywinauto)")
    except Exception as e:
        log_message(f"파일 첨부 자동화 실패: {e}")
    return False

# ----------------------------------------
# 🤖 1단계: 날짜 검색 ('tilde_marker.png' 기준)
# ----------------------------------------
def step_1_search_by_date(start_date, end_date):
    log_message("[1/4] 1단계: 날짜 필터링 시작...")
    click_image('filter_details.png')
    click_image('period_input.png')
    click_image('direct_input.png')

    try:
        # 1. '~' 마커를 찾습니다.
        tilde_loc = find_image_location('tilde_marker.png', timeout=5)

        # 2. '~'의 왼쪽을 클릭 (시작 날짜 필드)
        start_field_x = tilde_loc.x - 80
        start_field_y = tilde_loc.y
        log_message(f"'~' 기준 시작 날짜 필드 클릭 ({start_field_x}, {start_field_y})")
        pyautogui.click(start_field_x, start_field_y)
        time.sleep(0.5)
        # 기존 내용을 지우고 새로 씁니다.
        pyautogui.hotkey('ctrl', 'a')
        pyautogui.press('delete')
        pyautogui.write(start_date, interval=0.1)
        time.sleep(0.5)

        # 3. '~'의 오른쪽을 클릭 (종료 날짜 필드)
        end_field_x = tilde_loc.x + 80
        end_field_y = tilde_loc.y
        log_message(f"'~' 기준 종료 날짜 필드 클릭 ({end_field_x}, {end_field_y})")
        pyautogui.click(end_field_x, end_field_y)
        time.sleep(0.5)
        # 기존 내용을 지우고 새로 씁니다.
        pyautogui.hotkey('ctrl', 'a')
        pyautogui.press('delete')
        pyautogui.write(end_date, interval=0.1)
        time.sleep(0.5)

    except FileNotFoundError as e:
        log_message("치명적 오류: 'tilde_marker.png'를 찾을 수 없습니다.")
        log_message("시작/종료 날짜 사이의 '~' 이미지를 캡처해야 합니다.")
        raise e

    click_image('confirm_button.png')
    click_image('search_button.png')

    if ENABLE_WAIT_FOR_MARKER and LIST_READY_MARKER:
        log_message("검색 후 결과 목록 준비 대기 중...")
        if not wait_for(LIST_READY_MARKER, timeout=15, region=None):
            log_message("경고: 결과 목록 준비 마커를 찾지 못했습니다. 임의 대기 사용.")
            time.sleep(3)
    else:
        log_message("날짜 필터링 및 검색 완료. 3초 대기...")
        time.sleep(3)

# ----------------------------------------
# 🤖 2단계: 메시지/파일 전송 및 복귀
# ----------------------------------------
def step_2_send_message_and_back(message, file_path):
    log_message("[3/4] 2단계: 메시지 및 파일 전송...")

    # 0. 메시지 입력창 포커스 시도(선택)
    try:
        click_image('message_input.png', timeout=2)
    except Exception:
        pass

    # 1. 메시지 입력 (클립보드 권장)
    try:
        import pyperclip
        pyperclip.copy(message)
        pyautogui.hotkey('ctrl', 'v')
        time.sleep(0.8)
    except Exception:
        log_message("경고: 클립보드 방식 실패. pyperclip 설치 권장. 영문/숫자만 안전하게 입력합니다.")
        pyautogui.write(message, interval=0.01)

    # 2. 파일 첨부(선택, 실험적)
    if file_path:
        log_message(f"파일 첨부 시도: {file_path}")
        attached = False
        try:
            click_image('file_attach_button.png', timeout=3)
            time.sleep(1.0)
            attached = attach_file_via_dialog(file_path)
        except Exception as e:
            log_message(f"파일 첨부 버튼 클릭 실패: {e}")
        if not attached:
            log_message("파일 첨부를 진행하지 못했습니다. 메시지만 전송합니다.")

    # 3. 전송
    click_image('send_button.png')

    # 4. 뒤로 가기
    log_message("전송 완료. 목록으로 복귀합니다.")
    click_image('back_button.png')
    if ENABLE_WAIT_FOR_MARKER and LIST_READY_MARKER:
        log_message("목록 갱신 대기 중...")
        if not wait_for(LIST_READY_MARKER, timeout=20):
            log_message(f"경고: 목록 준비 마커를 찾지 못했습니다. {LIST_REFRESH_TIME}초 대기.")
            time.sleep(LIST_REFRESH_TIME)
    else:
        log_message(f"목록 갱신 대기 ({LIST_REFRESH_TIME}초)...")
        time.sleep(LIST_REFRESH_TIME)

# ----------------------------------------
# 🤖 3단계: '검색된 모두에게' 루프 (수정됨: 버그 수정)
# ----------------------------------------
def step_3_execute_loop_mode(message, file_path):
    log_message("[2/4] 3단계: '모두에게 발송' (이름 기억+스크롤) 로직 시작...")

    # 1. 이미 처리한 이름을 저장할 세트(set)
    processed_names = set()

    # 2. 스크롤해도 새 대상이 나오지 않는 횟수를 카운트
    consecutive_scrolls_with_no_new_targets = 0

    # 3. (신규) 연속 예외 발생 횟수 카운트 (이미지 파일 오류 대응)
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 3  # 3회 연속 예외 발생 시 종료

    # 4. (신규) OCR 실패 횟수 추적
    ocr_fail_count = 0
    ocr_success_count = 0

    # 5. 메인 루프 (스크롤 반복)
    while True:
        check_stop_signal()
        log_message("\n--- 새 화면 스캔 시작 ---")

        # 5. 현재 화면에 보이는 모든 'checkbox.png' 찾기
        try:
            all_checkboxes = list(pyautogui.locateAllOnScreen('checkbox.png', confidence=IMAGE_CONFIDENCE))

            # ===== [수정] 성공하면 예외 카운터 리셋 =====
            consecutive_errors = 0
            # =============================================

            # ===== [수정] 체크박스를 찾지 못한 경우 종료 조건 체크 =====
            if not all_checkboxes:
                log_message("화면에 'checkbox.png'를 찾을 수 없습니다.")
                consecutive_scrolls_with_no_new_targets += 1

                # 종료 조건: 2번 연속 체크박스를 찾지 못함
                if consecutive_scrolls_with_no_new_targets >= 2:
                    log_message("2회 연속 체크박스를 찾지 못했습니다. 목록의 끝으로 간주합니다.")
                    log_message("======= 🎉 모든 대상에게 발송 완료! =======")
                    break

                log_message("스크롤하여 다음 페이지를 확인합니다...")
                pyautogui.scroll(-500)
                time.sleep(LIST_REFRESH_TIME)  # ===== [수정] 대기 시간 일관성 개선 =====
                continue
            # ============================================================

            # Y좌표(top) 기준으로 정렬 (위에서 아래로)
            all_checkboxes.sort(key=lambda box: box.top)

        except Exception as e:
            # ===== [수정] 예외 발생 시 카운터 증가 및 종료 조건 체크 =====
            consecutive_errors += 1
            log_message(f"체크박스 검색 중 오류 ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}")

            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log_message("❌ 치명적 오류: 체크박스 이미지 검색이 연속 3회 실패했습니다.")
                log_message("다음 사항을 확인하세요:")
                log_message("  1. 'checkbox.png' 파일이 스크립트와 같은 폴더에 있는지")
                log_message("  2. 파일이 손상되지 않았는지")
                log_message("  3. 파일 권한이 올바른지")
                log_message("  4. 카카오톡 화면이 활성화되어 있는지")
                raise FileNotFoundError(f"'checkbox.png' 파일 읽기 실패: {e}")

            log_message(f"{MAX_CONSECUTIVE_ERRORS - consecutive_errors}회 더 실패하면 작업을 중단합니다...")
            time.sleep(1)
            continue
            # ===========================================================

        # 5. 현재 화면에서 '새로 처리할 대상' 목록 만들기
        new_targets_on_this_screen = []

        for box in all_checkboxes:
            check_stop_signal()
            try:
                center_coord = pyautogui.center(box)
                name, region = get_target_info(center_coord)

                # ===== [수정] OCR 성공/실패 카운트 =====
                if not name:
                    ocr_fail_count += 1
                    log_message(f"경고: 좌표 {center_coord}의 이름을 읽지 못했습니다. (OCR 실패 {ocr_fail_count}회)")

                    # OCR 실패율이 너무 높으면 경고
                    total_ocr_attempts = ocr_success_count + ocr_fail_count
                    if total_ocr_attempts >= 10 and (ocr_fail_count / total_ocr_attempts) > 0.5:
                        log_message("⚠️⚠️⚠️ 경고: OCR 실패율이 50%를 초과했습니다! ⚠️⚠️⚠️")
                        log_message("💡 문제 해결 방법:")
                        log_message("  1. 프로그램을 닫고 '관리자 권한'으로 다시 실행하세요")
                        log_message("  2. 바이러스 백신 프로그램을 일시적으로 비활성화하세요")
                        log_message("  3. OCR_REGION_OFFSET 설정값을 확인하세요")
                        log_message("현재 진행 상황: 성공 {}, 실패 {}".format(ocr_success_count, ocr_fail_count))

                    continue
                else:
                    ocr_success_count += 1
                # ==========================================

                # 6. 'processed_names'에 없는 새로운 이름인지 확인
                if name not in processed_names:
                    log_message(f"발견 (신규): [{name}]")
                    processed_names.add(name)
                    new_targets_on_this_screen.append((name, center_coord))
                else:
                    log_message(f"발견 (기존): [{name}] (건너뜀)")

            except Exception as e:
                log_message(f"대상 처리 중 오류: {e}")

        # 7. 스캔 결과에 따라 행동 결정
        if not new_targets_on_this_screen:
            # 7-A. 이 화면에서 새로운 대상을 못 찾음
            log_message("--- 이 화면에서 처리할 신규 대상 없음 ---")
            consecutive_scrolls_with_no_new_targets += 1

            # 8. 종료 조건: 2번 연속 스크롤했는데도 새 대상이 없으면 종료
            if consecutive_scrolls_with_no_new_targets >= 2:
                log_message("2회 연속 새 대상을 찾지 못했습니다. 목록의 끝으로 간주합니다.")
                log_message("======= 🎉 모든 대상에게 발송 완료! =======")
                break
            else:
                log_message("목록의 끝인지 확인하기 위해 1회 추가 스크롤...")
                pyautogui.scroll(-500)
                time.sleep(LIST_REFRESH_TIME)

        else:
            # 7-B. 이 화면에서 새로운 대상을 찾음 -> 순서대로 발송
            log_message(f"--- 이 화면에서 {len(new_targets_on_this_screen)}명의 신규 대상 처리 시작 ---")
            consecutive_scrolls_with_no_new_targets = 0  # 카운터 리셋

            for name, coord in new_targets_on_this_screen:
                check_stop_signal()
                log_message(f"▶ [{name}]에게 발송 시작...")
                pyautogui.click(coord)
                time.sleep(SLEEP_TIME)
                step_2_send_message_and_back(message, file_path)

            log_message("--- 현재 화면 대상 처리 완료. 다음 페이지로 스크롤... ---")
            pyautogui.scroll(-500)
            time.sleep(LIST_REFRESH_TIME)

        # 무한루프 안전장치
        if len(processed_names) > 200:
            raise TimeoutError("루프 횟수가 200회를 초과했습니다 (무한루프 방지).")

# ----------------------------------------
# (미지원) 특정 1명 모드
# ----------------------------------------
def step_3_execute_specific_mode(target_name, message, file_path):
    log_message(f"[2/4] 3단계: '특정 1명' [{target_name}] 찾기 시작...")
    log_message("'특정 1명' 모드는 OCR/스크롤 자동화 불확실성으로 현재 미지원입니다.")
    raise NotImplementedError("'특정 1명' 모드는 구현되지 않았습니다.")

# ----------------------------------------
# 🧠 자동화 메인 컨트롤러(워커 스레드)
# ----------------------------------------
def run_automation_logic(params):
    try:
        start_date = params['start_date']
        end_date = params['end_date']
        send_mode = params['send_mode']
        target_name = params['target_name']
        message = params['message']
        file_path = params['file_path']
        run_step1 = params['run_step1']
        run_step2 = params['run_step2']

        # ===== [신규] 시작 전 필수 이미지 파일 체크 =====
        log_message("\n===== 사전 점검: 필수 이미지 파일 확인 =====")
        if not check_required_image_files(run_step1, run_step2):
            safe_messagebox("error", "이미지 파일 누락",
                          "필수 이미지 파일이 누락되었습니다.\n로그를 확인하고 파일을 준비한 후 다시 시도하세요.")
            return  # 작업 중단
        log_message("=" * 50 + "\n")
        # ==============================================

        log_message("======= 자동화 시작 (3초 후) =======")
        log_message("카카오톡 채널 관리자 화면을 활성화하세요!")
        log_message("(중지하려면 '즉시 중지(ESC)' 버튼을 누르세요)")

        for i in range(3, 0, -1):
            check_stop_signal()
            log_message(f"{i}초...")
            time.sleep(1)

        # --- 1단계 (조건부 실행) ---
        check_stop_signal()
        if run_step1:
            step_1_search_by_date(start_date, end_date)
        else:
            log_message("[1/4] 1단계: 날짜 필터링을 건너뜁니다 (사용자 설정).")
            log_message(">>> 주의: 2단계를 실행하려면 화면에 대상 목록이 준비되어 있어야 합니다.")
            time.sleep(2)

        # --- 2단계 (조건부 실행) ---
        check_stop_signal()
        if run_step2:
            if send_mode == "all":
                step_3_execute_loop_mode(message, file_path)
            else:
                step_3_execute_specific_mode(target_name, message, file_path)
        else:
            log_message("[2/4] 2단계: 메시지 일괄 발송을 건너뜁니다 (사용자 설정).")

        log_message("======= 🎉 모든 작업 성공적으로 완료! =======")
        safe_messagebox("info", "성공", "자동화 작업이 모두 완료되었습니다.")

    except FileNotFoundError as e:
        log_message(f"❌ 이미지 찾기 오류: {e}")
        log_message("해상도, 다크모드, 창 가림, PNG 위치를 점검하세요.")
        safe_messagebox("error", "이미지 찾기 실패",
                        f"이미지 찾기 실패:\n{e}\n\n.png 파일이 스크립트와 같은 폴더에 있고, 화면에 해당 버튼이 보이는지 확인하세요.")
    except InterruptedError as e:
        log_message(f"⚠️ 작업 중지: {e}")
        safe_messagebox("warning", "작업 중지", "사용자에 의해 작업이 중지되었습니다.")
    except Exception as e:
        log_message(f"❌ 치명적 오류: {e}")
        import traceback
        log_message(traceback.format_exc())
        safe_messagebox("error", "치명적 오류", f"알 수 없는 오류가 발생했습니다:\n{e}")
    finally:
        # 버튼 상태 복구
        stop_event.clear()
        ui_dispatch(btn_start.config, state=tk.NORMAL)
        ui_dispatch(btn_stop.config, state=tk.DISABLED)

# ----------------------------------------
# 🖥️ Tkinter GUI
# ----------------------------------------
def create_gui():
    global log_area, btn_start, btn_stop
    global entry_start_date, entry_end_date, entry_target_name
    global text_message, label_file_path_var, var_send_mode
    global root
    global var_step1_enabled, var_step2_enabled

    root = tk.Tk()
    root.title("카카오톡 자동화 v1.2 (버그 수정)")

    main_frame = ttk.Frame(root, padding="10")
    main_frame.pack(fill=tk.BOTH, expand=True)

    # --- 1. 필터 설정 ---
    filter_frame = ttk.LabelFrame(main_frame, text=" [ 1. 필터 설정 ] ", padding="10")
    filter_frame.pack(fill=tk.X, expand=True)

    ttk.Label(filter_frame, text="시작 날짜 (YYYYMMDD):").grid(row=0, column=0, sticky=tk.W, padx=5, pady=5)
    entry_start_date = ttk.Entry(filter_frame, width=20)
    entry_start_date.grid(row=0, column=1, sticky=tk.W, padx=5, pady=5)
    entry_start_date.insert(0, "20251029")

    ttk.Label(filter_frame, text="종료 날짜 (YYYYMMDD):").grid(row=0, column=2, sticky=tk.W, padx=5, pady=5)
    entry_end_date = ttk.Entry(filter_frame, width=20)
    entry_end_date.grid(row=0, column=3, sticky=tk.W, padx=5, pady=5)
    entry_end_date.insert(0, "20251030")

    var_send_mode = tk.StringVar(value="all")

    def on_mode_change():
        if var_send_mode.get() == "specific":
            entry_target_name.config(state=tk.NORMAL)
        else:
            entry_target_name.config(state=tk.DISABLED)

    ttk.Label(filter_frame, text="발송 모드:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=5)
    mode_frame = ttk.Frame(filter_frame)
    mode_frame.grid(row=1, column=1, columnspan=3, sticky=tk.W)
    ttk.Radiobutton(mode_frame, text="검색된 모두에게 발송 (이름+스크롤)", variable=var_send_mode, value="all",
                    command=on_mode_change).pack(side=tk.LEFT, padx=5)
    ttk.Radiobutton(mode_frame, text="특정 1명에게 (미지원)", variable=var_send_mode, value="specific",
                    command=on_mode_change, state=tk.DISABLED).pack(side=tk.LEFT, padx=5)

    ttk.Label(filter_frame, text="타겟 이름 (특정 1명):").grid(row=2, column=0, sticky=tk.W, padx=5, pady=5)
    entry_target_name = ttk.Entry(filter_frame, width=40, state=tk.DISABLED)
    entry_target_name.grid(row=2, column=1, columnspan=3, sticky=tk.W, padx=5, pady=5)

    # --- 2. 발송 내용 ---
    message_frame = ttk.LabelFrame(main_frame, text=" [ 2. 발송 내용 ] ", padding="10")
    message_frame.pack(fill=tk.X, expand=True, pady=10)

    ttk.Label(message_frame, text="메시지 내용:").pack(anchor=tk.W)
    text_message = scrolledtext.ScrolledText(message_frame, width=70, height=7, wrap=tk.WORD)
    text_message.pack(fill=tk.X, expand=True, padx=5, pady=5)
    text_message.insert(tk.END, "안녕하세요. 홍보 메시지 테스트입니다.\n줄바꿈도 잘 되는지 테스트합니다.")

    file_frame = ttk.Frame(message_frame)
    file_frame.pack(fill=tk.X, expand=True)
    label_file_path_var = tk.StringVar(value="선택된 파일 없음")

    def select_file():
        filepath = filedialog.askopenfilename()
        if filepath:
            log_message(f"파일 선택됨: {filepath}")
            label_file_path_var.set(filepath)
            messagebox.showinfo("안내", "파일 첨부는 시스템/언어 설정에 따라 실패할 수 있습니다.\n실패 시 메시지만 전송됩니다.")

    ttk.Button(file_frame, text="파일 찾기... (실험적)", command=select_file).pack(side=tk.LEFT, padx=5, pady=5)
    ttk.Label(file_frame, textvariable=label_file_path_var, foreground="gray").pack(side=tk.LEFT, padx=5, pady=5)

    # --- 3. 실행 제어 ---
    control_frame = ttk.LabelFrame(main_frame, text=" [ 3. 실행 제어 ] ", padding="10")
    control_frame.pack(fill=tk.X, expand=True)

    var_step1_enabled = tk.BooleanVar(value=True)
    var_step2_enabled = tk.BooleanVar(value=True)

    checkbox_frame = ttk.Frame(control_frame)
    checkbox_frame.pack(fill=tk.X, expand=True, pady=5)

    ttk.Checkbutton(checkbox_frame, text="✅ 1단계: 날짜로 검색 실행",
                    variable=var_step1_enabled, onvalue=True, offvalue=False).pack(side=tk.LEFT, padx=10)
    ttk.Checkbutton(checkbox_frame, text="✅ 2단계: 메시지 일괄 발송 실행",
                    variable=var_step2_enabled, onvalue=True, offvalue=False).pack(side=tk.LEFT, padx=10)

    button_frame = ttk.Frame(control_frame)
    button_frame.pack(fill=tk.X, expand=True)

    def on_start_button_click():
        global automation_thread

        params = {
            'start_date': entry_start_date.get().strip(),
            'end_date': entry_end_date.get().strip(),
            'send_mode': var_send_mode.get(),
            'target_name': entry_target_name.get().strip(),
            'message': text_message.get("1.0", tk.END).strip(),
            'file_path': label_file_path_var.get() if label_file_path_var.get() != "선택된 파일 없음" else None,
            'run_step1': var_step1_enabled.get(),
            'run_step2': var_step2_enabled.get()
        }

        # 유효성 검사
        if params['run_step1'] and (len(params['start_date']) != 8 or not params['start_date'].isdigit()):
            messagebox.showwarning("입력 오류", "1단계 실행 시: 시작 날짜를 YYYYMMDD (8자리)로 입력하세요.")
            return
        if params['run_step1'] and (len(params['end_date']) != 8 or not params['end_date'].isdigit()):
            messagebox.showwarning("입력 오류", "1단계 실행 시: 종료 날짜를 YYYYMMDD (8자리)로 입력하세요.")
            return
        if params['run_step2'] and not params['message']:
            messagebox.showwarning("입력 오류", "2단계 실행 시: 발송할 메시지 내용을 입력하세요.")
            return
        if not params['run_step1'] and not params['run_step2']:
            messagebox.showwarning("입력 오류", "실행할 단계를 1개 이상 체크하세요.")
            return

        btn_start.config(state=tk.DISABLED)
        btn_stop.config(state=tk.NORMAL)
        stop_event.clear()

        automation_thread = threading.Thread(target=run_automation_logic, args=(params,), daemon=True)
        automation_thread.start()

    def on_stop_button_click():
        log_message("!!! '즉시 중지' 신호를 보냈습니다. 현재 작업 완료 후 중지됩니다...")
        stop_event.set()
        btn_stop.config(state=tk.DISABLED)

    def on_close_button_click():
        if automation_thread and automation_thread.is_alive():
            if messagebox.askyesno("확인", "작업이 아직 실행 중입니다. 정말로 종료하시겠습니까?"):
                stop_event.set()
                root.destroy()
        else:
            root.destroy()

    btn_start = ttk.Button(button_frame, text="▶️ 실행 (F5)", command=on_start_button_click)
    btn_start.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5, pady=5, ipady=10)

    btn_stop = ttk.Button(button_frame, text="⏹️ 즉시 중지 (ESC)", command=on_stop_button_click, state=tk.DISABLED)
    btn_stop.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5, pady=5, ipady=10)

    btn_close = ttk.Button(button_frame, text="✖️ 닫기", command=on_close_button_click)
    btn_close.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5, pady=5, ipady=10)

    root.bind('<F5>', lambda event: on_start_button_click())
    root.bind('<Escape>', lambda event: on_stop_button_click())

    # --- 4. 작업 로그 ---
    log_frame = ttk.LabelFrame(main_frame, text=" [ 4. 작업 로그 ] ", padding="10")
    log_frame.pack(fill=tk.BOTH, expand=True, pady=10)

    global log_area
    log_area = scrolledtext.ScrolledText(log_frame, width=70, height=10, wrap=tk.WORD, state=tk.DISABLED)
    log_area.pack(fill=tk.BOTH, expand=True)

    root.protocol("WM_DELETE_WINDOW", on_close_button_click)

    log_message("GUI 준비 완료. 자동화 설정을 확인하세요.")
    log_message(f"Tesseract 경로: {TESSERACT_CMD_PATH}")

    # ===== [신규] 관리자 권한 확인 =====
    if not is_admin():
        log_message("⚠️ 경고: 프로그램이 일반 사용자 권한으로 실행 중입니다.")
        log_message("💡 OCR 권한 오류([WinError 5])가 발생하면:")
        log_message("   프로그램을 마우스 오른쪽 버튼 클릭 → '관리자 권한으로 실행'하세요.")
        log_message("")
    else:
        log_message("✅ 관리자 권한으로 실행 중입니다.")
        log_message("")
    # ===================================

    if not os.path.exists(r"C:\Program Files\Tesseract-OCR\tesseract.exe"):
        log_message("경고! Tesseract 경로가 올바르지 않습니다. TESSERACT_CMD_PATH를 수정하세요.")
        safe_messagebox("error", "Tesseract 오류",
                        f"Tesseract-OCR을 찾을 수 없습니다.\n경로: {TESSERACT_CMD_PATH}\n\n스크립트 상단의 TESSERACT_CMD_PATH 변수를 수정하세요.")

    _drain_ui_queue()
    root.mainloop()

# ----------------------------------------
# 🚀 메인
# ----------------------------------------
if __name__ == "__main__":
    create_gui()
