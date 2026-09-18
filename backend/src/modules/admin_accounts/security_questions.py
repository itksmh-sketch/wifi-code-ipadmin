"""Fixed list of security questions. Stored on admin_users by key, so wording
can be edited here without touching data; never reuse or repurpose a key."""
import hashlib
import unicodedata

SECURITY_QUESTIONS: dict[str, str] = {
    "first_school": "What was the name of your first school?",
    "birth_town": "In which town were you born?",
    "mother_maiden_name": "What is your mother's maiden name?",
    "first_employer": "What was the name of your first employer?",
    "childhood_friend": "What is the first name of your childhood best friend?",
    "favourite_teacher": "What was the surname of your favourite teacher?",
}

ANSWER_MIN_LENGTH = 2


def normalize_answer(answer: str) -> str:
    """Case-, accent-width- and whitespace-insensitive form that gets hashed."""
    text = unicodedata.normalize("NFKC", answer or "")
    return " ".join(text.casefold().split())


def decoy_question_key(email: str) -> str:
    """A stable question for emails with no usable account, so the endpoint
    answers identically whether or not the account exists."""
    digest = hashlib.sha256((email or "").strip().lower().encode()).digest()
    keys = sorted(SECURITY_QUESTIONS)
    return keys[digest[0] % len(keys)]
