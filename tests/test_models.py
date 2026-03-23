"""Тесты моделей данных."""

from app.models import (
    Folder, Meeting, Transcript, Summary, ChatMessage,
    MeetingSource, MeetingStatus, ChatRole,
)


def test_create_folder(db_session):
    folder = Folder(name="Разработка", icon="💻")
    db_session.add(folder)
    db_session.commit()
    db_session.refresh(folder)

    assert folder.id is not None
    assert folder.name == "Разработка"
    assert folder.icon == "💻"
    assert folder.created_at is not None


def test_folder_default_icon(db_session):
    folder = Folder(name="Тест")
    db_session.add(folder)
    db_session.commit()
    db_session.refresh(folder)

    assert folder.icon == "📁"


def test_delete_folder(db_session):
    folder = Folder(name="Удалить")
    db_session.add(folder)
    db_session.commit()

    db_session.delete(folder)
    db_session.commit()

    assert db_session.query(Folder).count() == 0


def test_create_meeting(db_session):
    folder = Folder(name="Продажи")
    db_session.add(folder)
    db_session.commit()

    meeting = Meeting(
        title="Демо клиенту",
        folder_id=folder.id,
        source=MeetingSource.upload,
        status=MeetingStatus.downloading,
    )
    db_session.add(meeting)
    db_session.commit()
    db_session.refresh(meeting)

    assert meeting.id is not None
    assert meeting.folder_id == folder.id
    assert meeting.source == MeetingSource.upload
    assert meeting.status == MeetingStatus.downloading


def test_meeting_status_update(db_session):
    meeting = Meeting(title="Тест", status=MeetingStatus.downloading)
    db_session.add(meeting)
    db_session.commit()

    meeting.status = MeetingStatus.transcribing
    db_session.commit()
    db_session.refresh(meeting)

    assert meeting.status == MeetingStatus.transcribing


def test_meeting_cascade_delete(db_session):
    meeting = Meeting(title="Каскад")
    db_session.add(meeting)
    db_session.commit()

    transcript = Transcript(meeting_id=meeting.id, full_text="Текст", segments=[])
    summary = Summary(meeting_id=meeting.id, tldr="Резюме")
    db_session.add_all([transcript, summary])
    db_session.commit()

    db_session.delete(meeting)
    db_session.commit()

    assert db_session.query(Transcript).count() == 0
    assert db_session.query(Summary).count() == 0


def test_folder_meetings_relationship(db_session):
    folder = Folder(name="Планёрки")
    db_session.add(folder)
    db_session.commit()

    m1 = Meeting(title="Планёрка 1", folder_id=folder.id)
    m2 = Meeting(title="Планёрка 2", folder_id=folder.id)
    db_session.add_all([m1, m2])
    db_session.commit()
    db_session.refresh(folder)

    assert len(folder.meetings) == 2


def test_create_transcript(db_session):
    meeting = Meeting(title="Тест")
    db_session.add(meeting)
    db_session.commit()

    transcript = Transcript(
        meeting_id=meeting.id,
        full_text="Привет, это тест",
        segments=[{"start": 0, "end": 5, "speaker": "Алмаз", "text": "Привет, это тест"}],
    )
    db_session.add(transcript)
    db_session.commit()
    db_session.refresh(transcript)

    assert transcript.full_text == "Привет, это тест"
    assert len(transcript.segments) == 1
    assert transcript.segments[0]["speaker"] == "Алмаз"


def test_create_summary(db_session):
    meeting = Meeting(title="Тест")
    db_session.add(meeting)
    db_session.commit()

    summary = Summary(
        meeting_id=meeting.id,
        tldr="Обсудили план на квартал",
        tasks=[{"task": "Сделать MVP", "assignee": "Алмаз", "deadline": "01.04"}],
        topics=[{"topic": "MVP", "details": "Решили делать на FastAPI"}],
        insights=[{"insight": "Нужно больше тестов"}],
    )
    db_session.add(summary)
    db_session.commit()
    db_session.refresh(summary)

    assert summary.tldr == "Обсудили план на квартал"
    assert len(summary.tasks) == 1
    assert summary.tasks[0]["assignee"] == "Алмаз"


def test_create_chat_message_for_meeting(db_session):
    meeting = Meeting(title="Тест")
    db_session.add(meeting)
    db_session.commit()

    msg = ChatMessage(meeting_id=meeting.id, role=ChatRole.user, content="Какие задачи?")
    db_session.add(msg)
    db_session.commit()

    assert msg.role == ChatRole.user
    assert msg.meeting_id == meeting.id
    assert msg.folder_id is None


def test_create_chat_message_for_folder(db_session):
    folder = Folder(name="Тест")
    db_session.add(folder)
    db_session.commit()

    msg = ChatMessage(folder_id=folder.id, role=ChatRole.assistant, content="Ответ AI")
    db_session.add(msg)
    db_session.commit()

    assert msg.role == ChatRole.assistant
    assert msg.folder_id == folder.id
