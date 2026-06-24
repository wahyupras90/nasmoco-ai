import subprocess
import time
import pyautogui
import pygetwindow as gw

NIS_EXE = r"C:\Users\Toyota\OneDrive\Desktop\NIS Service 5.5.47A.exe"

def is_nis_open():
    windows = gw.getAllTitles()
    for w in windows:
        if "NIS" in w or "Nasmoco" in w:
            return True
    return False

def buka_nis():
    print("NIS belum terbuka, membuka NIS...")
    subprocess.Popen(NIS_EXE)
    time.sleep(5)

    # Isi Kode Petugas
    pyautogui.click(660, 358)
    time.sleep(0.5)
    pyautogui.hotkey('ctrl', 'a')
    pyautogui.typewrite('KBG', interval=0.1)

    # Tab → tunggu nama muncul
    pyautogui.press('tab')
    time.sleep(1)

    # Isi Password
    pyautogui.click(762, 418)
    time.sleep(0.5)
    pyautogui.hotkey('ctrl', 'a')
    pyautogui.typewrite('qwer12345', interval=0.1)

    # Klik LOGIN
    pyautogui.click(683, 468)
    time.sleep(4)

    # Dismiss popup ActiveBar
    pyautogui.press('enter')
    time.sleep(1)
    print("NIS siap!")

def pastikan_nis_siap():
    if is_nis_open():
        print("NIS sudah terbuka, skip login.")
        windows = gw.getWindowsWithTitle('NIS')
        if windows:
            windows[0].activate()
            time.sleep(1)
    else:
        buka_nis()

pastikan_nis_siap()

def navigasi_unit_masuk():
    print("Navigasi ke Laporan Unit Masuk...")
    
    # Klik MENU UTAMA
    pyautogui.click(54, 35)
    time.sleep(1)
    
    # Klik Laporan
    pyautogui.click(60, 238)
    time.sleep(0.5)
    
    # Klik Laporan Jumlah Unit
    pyautogui.click(246, 238)
    time.sleep(0.5)
    
    # Klik Jumlah Unit Masuk
    pyautogui.click(413, 238)
    time.sleep(1)
    
    print("Form Laporan Unit Masuk terbuka!")

navigasi_unit_masuk()