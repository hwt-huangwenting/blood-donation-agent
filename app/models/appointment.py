"""报名草稿模型：create_appointment 工具的唯一写入口。

只存「草稿」，用户在前端确认卡片亲自填写健康征询并同意后，
才由现有后端接口正式报名（donation_registrations 表，Python 侧不直连）。
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AppointmentDraft(Base):
    """报名草稿：一次「用户 × 活动 × 献血类型」的预约意向。"""

    __tablename__ = "appointment_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 用户标识（字符串化的用户 id，由服务端从 JWT 注入，模型不可指定）
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 关联现有 donation_events 活动表的活动 id（不新建活动表）
    event_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # 冗余存活动标题，便于草稿展示时不依赖业务库
    event_title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # 献血类型：whole_blood（全血）/ component_blood（成分血）
    donation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # 幂等键：sha256(user_id:event_id:donation_type)，同一意向重复请求只生成一份草稿
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # 草稿状态：draft（待用户确认）/ submitted（已正式报名）/ cancelled
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    # 正式报名成功后，现有后端返回的 registrationId（donation_registrations.id）
    registration_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<AppointmentDraft user={self.user_id} event={self.event_id} {self.donation_type}>"
