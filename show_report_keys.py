
import os
import django
import sys
import json

# Setup Django
sys.path.append(os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from video_management.models import LarkReport

def show_answers():
    latest = LarkReport.objects.exclude(name='Unknown').order_by('-created_at').first()
    if latest:
        content = f"Report ID: {latest.id}\nAnswers:\n{json.dumps(latest.answers, ensure_ascii=False, indent=2)}"
        with open("answers_out.txt", "w", encoding="utf-8") as f:
            f.write(content)
        print("Success: Answers written to answers_out.txt")
    else:
        print("No reports found.")

if __name__ == "__main__":
    show_answers()
