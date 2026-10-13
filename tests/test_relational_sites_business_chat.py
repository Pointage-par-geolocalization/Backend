from app.models import (
    Base, Client, Site, SiteMember, SiteStep, SiteNote, BusinessDocument,
    DocumentLine, DocumentVersion, DocumentCounter, Chat, ChatMember,
    ChatMessage, MessageReaction, ChatRead,
)


def test_sites_clients_and_documents_are_relational():
    expected = {
        "clients", "sites", "site_members", "site_steps", "site_notes",
        "documents", "document_lines", "document_versions", "document_counters",
    }
    assert expected <= set(Base.metadata.tables)
    assert Client.__tablename__ == "clients"
    assert Site.__tablename__ == "sites"
    assert SiteMember.__tablename__ == "site_members"
    assert SiteStep.__tablename__ == "site_steps"
    assert SiteNote.__tablename__ == "site_notes"
    assert BusinessDocument.__tablename__ == "documents"
    assert DocumentLine.__tablename__ == "document_lines"
    assert DocumentVersion.__tablename__ == "document_versions"
    assert DocumentCounter.__tablename__ == "document_counters"

    assert {fk.target_fullname for fk in Site.__table__.foreign_keys} >= {
        "clients.id", "workplaces.id"
    }
    assert {fk.target_fullname for fk in SiteMember.__table__.foreign_keys} >= {
        "sites.id", "employees.employee_code"
    }
    assert {fk.target_fullname for fk in BusinessDocument.__table__.foreign_keys} >= {
        "clients.id", "sites.id", "documents.id"
    }
    assert {fk.target_fullname for fk in DocumentLine.__table__.foreign_keys} == {
        "documents.id"
    }
    assert {fk.target_fullname for fk in DocumentVersion.__table__.foreign_keys} == {
        "documents.id"
    }


def test_chat_members_messages_reactions_and_reads_are_relational():
    expected = {
        "chats", "chat_members", "chat_messages", "message_reactions", "chat_reads"
    }
    assert expected <= set(Base.metadata.tables)
    assert Chat.__tablename__ == "chats"
    assert ChatMember.__tablename__ == "chat_members"
    assert ChatMessage.__tablename__ == "chat_messages"
    assert MessageReaction.__tablename__ == "message_reactions"
    assert ChatRead.__tablename__ == "chat_reads"

    assert {fk.target_fullname for fk in ChatMember.__table__.foreign_keys} == {
        "chats.id", "users.id"
    }
    assert {fk.target_fullname for fk in ChatMessage.__table__.foreign_keys} == {
        "chats.id", "users.id"
    }
    assert {fk.target_fullname for fk in MessageReaction.__table__.foreign_keys} == {
        "chat_messages.id", "users.id"
    }
    assert {fk.target_fullname for fk in ChatRead.__table__.foreign_keys} == {
        "chats.id", "users.id"
    }
    assert "uq_chats_direct_user" in {index.name for index in Chat.__table__.indexes}
