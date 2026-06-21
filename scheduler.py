import json
import sys

from routes.admin import run_scheduled_reminders


def main():
    job = sys.argv[1].strip().lower() if len(sys.argv) > 1 else "all"
    result = run_scheduled_reminders(job)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
