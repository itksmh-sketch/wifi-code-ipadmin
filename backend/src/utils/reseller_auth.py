from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from src.config import get_settings

settings = get_settings()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_reseller_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=settings.reseller_jwt_expiration_minutes))
    to_encode.update(
        {
            "exp": expire,
            "type": "access",
            "iss": settings.reseller_jwt_issuer,
        }
    )
    return jwt.encode(to_encode, settings.reseller_jwt_secret, algorithm="HS256")


def create_reseller_refresh_token(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(days=7)
    to_encode.update(
        {
            "exp": expire,
            "type": "refresh",
            "iss": settings.reseller_jwt_issuer,
        }
    )
    return jwt.encode(to_encode, settings.reseller_jwt_secret, algorithm="HS256")


def verify_reseller_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.reseller_jwt_secret, algorithms=["HS256"])
        if payload.get("iss") != settings.reseller_jwt_issuer:
            return None
        if not payload.get("isp_operator_id"):
            return None
        return payload
    except JWTError:
        return None


def hash_reseller_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_reseller_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)
