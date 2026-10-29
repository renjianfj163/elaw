"""审查记录。原理图规则建议会写在这里。"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ReviewLog(Base):
    __tablename__ = "review_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    finding_id: Mapped[str | None] = mapped_column(String(36))
    target_type: Mapped[str] = mapped_column(String(40))
    target_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(40))
    by: Mapped[str] = mapped_column(String(100))
    at: Mapped[datetime] = mapped_column(DateTime)
    note: Mapped[str | None] = mapped_column(Text)
