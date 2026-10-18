"""SQLAlchemy 声明基类。所有模型继承 Base，统一挂到 Base.metadata 上建表。"""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """声明式基类。"""
    pass
