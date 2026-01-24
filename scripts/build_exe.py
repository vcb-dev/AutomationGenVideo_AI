#!/usr/bin/env python3
"""
Build script để tạo file exe từ tikhub_search_cli.py
Sử dụng PyInstaller
"""
import subprocess
import sys
import os

def build_exe():
    """Build exe file từ tikhub_search_cli.py"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cli_script = os.path.join(script_dir, 'tikhub_search_cli.py')
    
    if not os.path.exists(cli_script):
        print(f"Error: {cli_script} not found")
        return 1
    
    # PyInstaller command
    cmd = [
        'pyinstaller',
        '--onefile',  # Tạo single executable file
        '--name', 'tikhub_search',  # Tên file exe
        '--console',  # Console application
        '--clean',  # Clean cache
        '--noconfirm',  # Overwrite without confirmation
        cli_script
    ]
    
    print("Building executable...")
    print(f"Command: {' '.join(cmd)}")
    
    try:
        result = subprocess.run(cmd, check=True, cwd=script_dir)
        print("\n✓ Build successful!")
        print(f"✓ Executable: {os.path.join(script_dir, 'dist', 'tikhub_search.exe')}")
        return 0
    except subprocess.CalledProcessError as e:
        print(f"\n✗ Build failed: {e}")
        return 1
    except FileNotFoundError:
        print("\n✗ PyInstaller not found. Install it with:")
        print("  pip install pyinstaller")
        return 1


if __name__ == '__main__':
    sys.exit(build_exe())
