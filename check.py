import sys, os, traceback
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

print("1. Python:", sys.version)
print("2. CWD:", os.getcwd())

try:
    import tkinter
    print("3. tkinter: OK")
except:
    print("3. tkinter: MISSING!")
    traceback.print_exc()

try:
    import lxml
    print("4. lxml: OK")
except:
    print("4. lxml: MISSING!")

try:
    import requests
    print("5. requests: OK")
except:
    print("5. requests: MISSING!")

print("6. sys.path:", sys.path[:3])

try:
    SCRIPT_DIR = os.path.split(os.path.realpath(__file__))[0]
except:
    SCRIPT_DIR = os.getcwd()
print("7. SCRIPT_DIR:", SCRIPT_DIR)

try:
    sys.path.insert(0, SCRIPT_DIR)
    import const
    print("8. const import: OK, MODE =", const.MODE)
except Exception as e:
    print("8. const import: FAILED", e)

try:
    from weibo import Weibo, get_config
    print("9. weibo import: OK")
except Exception as e:
    print("9. weibo import: FAILED")
    traceback.print_exc()

try:
    import gui
    print("10. gui import: OK")
except Exception as e:
    print("10. gui import: FAILED")
    traceback.print_exc()

input("按回车退出...")
