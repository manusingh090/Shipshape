"""The demo accounts. Only used when DEMO_MODE is on.

`cookie` is the session key the seed script writes straight into the session
table, so the acceptance checker can send "Cookie: session=<value>" without
logging in. Accounts without a cookie still appear on the login page.
"""

from .models import User

DEMO_LOGINS = [
    {
        "key": "organizer",
        "lookup": {"email": "organizer@example.org"},
        "cookie": "org_3c9e51d7a2",
        "role": "Organizer",
    },
    {
        "key": "judge_a",
        "lookup": {"external_id": "jdg_24"},
        "cookie": "jdg_a_8f14c2e6",
        "role": "Judge",
    },
    {
        "key": "judge_b",
        "lookup": {"external_id": "jdg_26"},
        "cookie": "jdg_b_52e0b9d1",
        "role": "Judge",
    },
    {
        "key": "participant",
        "lookup": {"email": "priya1@example.org"},
        "cookie": "prt_91d7aa3f",
        "role": "Participant",
    },
    {
        "key": "admin",
        "lookup": {"email": "admin@example.org"},
        "cookie": "adm_6b2f03c8",
        "role": "Admin",
    },
    {
        "key": "newcomer",
        "lookup": {"email": "tom@example.org"},
        "cookie": None,
        "role": "Participant, no team yet",
    },
    {
        # Whichever judge the seeded top-up batch left with work to do.
        "key": "judge_todo",
        "lookup": {"judging_assignments__score__isnull": True},
        "cookie": None,
        "role": "Judge with reviews still to do",
    },
]


def demo_user(key):
    for entry in DEMO_LOGINS:
        if entry["key"] == key:
            return User.objects.filter(is_active=True, **entry["lookup"]).first()
    return None


def demo_accounts():
    """Entries that exist in the database, with the user attached."""
    found = []
    for entry in DEMO_LOGINS:
        user = User.objects.filter(is_active=True, **entry["lookup"]).first()
        if user:
            found.append({**entry, "user": user})
    return found
