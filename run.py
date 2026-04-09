#!/usr/bin/env python3
import sys, subprocess

def check_deps():
    missing = []
    for pkg, imp in [('flask', 'flask'), ('flask_sock', 'flask_sock'), ('werkzeug', 'werkzeug')]:
        try:
            __import__(imp)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"Installing: {', '.join(missing)}...")
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--break-system-packages', '-q'] + missing)

check_deps()

from app import create_app

app = create_app()

if __name__ == '__main__':
    print('\033[1;33m')
    print(' ╔══════════════════════════════════════╗')
    print(' ║  YT → Rutube Transfer Platform v5.0 ║')
    print(' ║  Auth · Admin · AI Dub · Shorts      ║')
    print(' ╚══════════════════════════════════════╝')
    print('\033[0m')
    print(f'  \033[1;32m✔\033[0m  http://localhost:5000')
    print(f'  \033[1;33m⚠\033[0m  admin / admin123  — смени пароль после входа!\n')
    import threading, webbrowser
    threading.Timer(1.2, lambda: webbrowser.open('http://localhost:5000')).start()
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
