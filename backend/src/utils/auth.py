from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
from passlib.context import CryptContext
from src.config import get_settings

settings = get_settings()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=settings.jwt_expiration_minutes))
    to_encode.update({"exp": expire, "type": "access", "iss": "admin"})
    return jwt.encode(to_encode, settings.jwt_secret, algorithm="HS256")


def create_refresh_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=7)
    to_encode.update({"exp": expire, "type": "refresh", "iss": "admin"})
    return jwt.encode(to_encode, settings.jwt_secret, algorithm="HS256")


def create_platform_owner_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=settings.platform_owner_jwt_expiration_minutes))
    to_encode.update({"exp": expire, "type": "access", "iss": "platform_owner"})
    return jwt.encode(to_encode, settings.platform_owner_jwt_secret, algorithm="HS256")


def create_platform_owner_refresh_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=7)
    to_encode.update({"exp": expire, "type": "refresh", "iss": "platform_owner"})
    return jwt.encode(to_encode, settings.platform_owner_jwt_secret, algorithm="HS256")


def verify_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        if payload.get("iss") != "admin":
            return None
        if not payload.get("isp_operator_id"):
            return None
        return payload
    except JWTError:
        return None


def verify_platform_owner_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.platform_owner_jwt_secret, algorithms=["HS256"])
        if payload.get("iss") != "platform_owner":
            return None
        return payload
    except JWTError:
        return None


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


# Password-reset grant: proof that an admin passed OTP or security-question
# verification, exchangeable once for a new password. A distinct issuer means
# verify_token() (which requires iss == "admin") can never accept it as a login
# session. It carries the admin's token_version, and setting the password bumps
# that version — so the grant is single-use and dies with any other reset.
PASSWORD_RESET_ISSUER = "admin_password_reset"
PASSWORD_RESET_TTL_MINUTES = 10


def create_password_reset_token(*, admin_id: str, token_version: int, via: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=PASSWORD_RESET_TTL_MINUTES)
    return jwt.encode(
        {"sub": admin_id, "tv": token_version, "via": via, "type": "password_reset",
         "iss": PASSWORD_RESET_ISSUER, "exp": expire},
        settings.jwt_secret,
        algorithm="HS256",
    )


def verify_password_reset_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"], issuer=PASSWORD_RESET_ISSUER)
    except JWTError:
        return None
    if payload.get("type") != "password_reset" or not payload.get("sub"):
        return None
    return payload


# Platform-owner login challenge: proof that the password was just verified,
# exchangeable once for a real token pair by answering the character-code
# challenge. A distinct issuer means verify_platform_owner_token() (which
# requires iss == "platform_owner") rejects it, so it can never act as a
# session at any protected route or at refresh. The distinct type is a second,
# independent guard. It carries the owner's token_version, and a jti that must
# equal platform_owners.challenge_pending_jti: every password success rotates
# that column, and every answer (right or wrong) clears it, so each challenge
# token is single-use and replacing it voids the previous one.
LOGIN_CHALLENGE_ISSUER = "platform_owner_login_challenge"
LOGIN_CHALLENGE_TYPE = "login_challenge"
LOGIN_CHALLENGE_TTL_MINUTES = 5


def create_login_challenge_token(*, owner_id: str, token_version: int, jti: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=LOGIN_CHALLENGE_TTL_MINUTES)
    return jwt.encode(
        {"sub": owner_id, "tv": token_version, "jti": jti, "type": LOGIN_CHALLENGE_TYPE,
         "iss": LOGIN_CHALLENGE_ISSUER, "exp": expire},
        settings.platform_owner_jwt_secret,
        algorithm="HS256",
    )


def verify_login_challenge_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.platform_owner_jwt_secret, algorithms=["HS256"], issuer=LOGIN_CHALLENGE_ISSUER)
    except JWTError:
        return None
    if payload.get("type") != LOGIN_CHALLENGE_TYPE or not payload.get("sub") or not payload.get("jti"):
        return None
    return payload
