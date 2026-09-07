"""Ukrainian display labels for English enum keys (answers doc Q19).

Nothing outside `ba_radar.render` should import this module. Storage, config and model
output all use the English keys.
"""

from __future__ import annotations

from ba_radar.models import Action, Category, Indicator, PracticeTag, Priority

CATEGORY_UK: dict[Category, str] = {
    Category.VENDOR: "Вендорське",
    Category.PRACTITIONER: "Практик",
    Category.NEWSLETTER: "Ньюзлетер",
    Category.RESEARCH: "Дослідження",
    Category.COMMUNITY: "Спільнота",
    Category.BA_SOURCE: "BA-джерело",
    Category.REGIONAL: "Регіональне",
}

PRACTICE_TAG_UK: dict[PracticeTag, str] = {
    PracticeTag.SPECIFICATIONS: "Специфікації",
    PracticeTag.CONTEXT_ENGINEERING: "Контекст-інженерія",
    PracticeTag.AGENT_ORCHESTRATION: "Оркестрація агентів",
    PracticeTag.EVALS: "Оцінювання",
    PracticeTag.TOOL_RELEASE: "Реліз інструменту",
    PracticeTag.ROLE_CHANGE: "Зміна ролей",
    PracticeTag.EVIDENCE_CRITIQUE: "Докази і критика",
    PracticeTag.SECURITY_QUALITY: "Безпека і якість",
}

PRIORITY_UK: dict[Priority, str] = {
    Priority.CRITICAL: "Критичний",
    Priority.NOTABLE: "Вартий уваги",
    Priority.BACKGROUND: "Фоновий",
}

ACTION_UK: dict[Action, str] = {
    Action.TRY: "Спробувати",
    Action.READ: "Прочитати",
    Action.NOTE: "Взяти до відома",
}

INDICATOR_UK: dict[Indicator, str] = {
    Indicator.LEADING: "Випереджальний",
    Indicator.LAGGING: "Запізнілий",
    Indicator.MIXED: "Змішаний",
}

# Digest chrome (PRD 3.1.3–3.1.5).
PROCESSED_UK = "Опрацьовано матеріалів"
CONTINUATION_UK = "продовження"
UNSCORED_BLOCK_UK = "Без аналізу"
INSIGHT_PREFIX_UK = "BA:"

NO_NEW_ITEMS = "Нових релевантних матеріалів за добу немає"
