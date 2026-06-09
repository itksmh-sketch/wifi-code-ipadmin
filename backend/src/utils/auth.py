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


def decode_jwt_any_issuer(token: str) -> Optional[dict]:
    for verifier in (verify_platform_owner_token, verify_token):
        payload = verifier(token)
        if payload is not None:
            return payload
    return None


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)
