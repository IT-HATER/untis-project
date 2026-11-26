# Vertretungsplan

Shows every lesson that is **suppliert** or **entfällt** for all classes,
from WebUntis — in the terminal and as a web page for a screen in the school.

## Start
    pip install -r requirements.txt
    python3 vertretungsplan.py --demo --serve   # try it with fake data
    # rename untis.env -> .env, fill it in, then:
    python3 vertretungsplan.py                  # terminal list
    python3 vertretungsplan.py --serve          # web page on port 8080, refreshes every 5 min

Options: `--days 3` (more days), `--html` (write vertretungsplan.html), `--port 8000`.

## Which account?
A student account only sees its own class. For all classes you need an account with
the right to see substitutions or class timetables (teacher, admin, or a display
account from the Untis admin). The program first tries the official substitution list
and falls back to reading each class timetable.
