from __future__ import annotations

import sqlite3

from .repositories.ai_decisions import AiDecisionsRepository
from .repositories.contacts import VacancyContactsRepository
from .repositories.employer_sites import EmployerSitesRepository
from .repositories.employers import EmployersRepository
from .repositories.events import EventsRepository
from .repositories.negotiations import NegotiationRepository
from .repositories.pending_messages import PendingMessagesRepository
from .repositories.resumes import ResumesRepository
from .repositories.settings import SettingsRepository
from .repositories.skipped_vacancies import SkippedVacanciesRepository
from .repositories.vacancies import VacanciesRepository
from .utils import init_db


class StorageFacade:
    """Единая точка доступа к persistence-слою."""

    def __init__(self, conn: sqlite3.Connection):
        init_db(conn)
        self.ai_decisions = AiDecisionsRepository(conn)
        self.employer_sites = EmployerSitesRepository(conn)
        self.employers = EmployersRepository(conn)
        self.events = EventsRepository(conn)
        self.negotiations = NegotiationRepository(conn)
        self.pending_messages = PendingMessagesRepository(conn)
        self.resumes = ResumesRepository(conn)
        self.settings = SettingsRepository(conn)
        self.skipped_vacancies = SkippedVacanciesRepository(conn)
        self.vacancies = VacanciesRepository(conn)
        self.vacancy_contacts = VacancyContactsRepository(conn)
