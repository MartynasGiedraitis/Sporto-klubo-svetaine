from pymongo import MongoClient, ASCENDING
from .config import MONGODB_URI, MONGODB_DATABASE

mongo_client = MongoClient(MONGODB_URI, tz_aware=True, serverSelectionTimeoutMS=15000)

def get_db():
    return mongo_client[MONGODB_DATABASE]

def init_database():
    db = get_db()
    db.command("ping")
    db.users.create_index("email", unique=True)
    db.memberships.create_index([("userId", ASCENDING), ("status", ASCENDING), ("expiresAt", ASCENDING)])
    db.training_sessions.create_index([("startsAt", ASCENDING), ("status", ASCENDING)])
    db.training_sessions.create_index([("trainerId", ASCENDING), ("startsAt", ASCENDING), ("endsAt", ASCENDING)])
    db.training_sessions.create_index([("roomId", ASCENDING), ("startsAt", ASCENDING), ("endsAt", ASCENDING)])
    db.bookings.create_index([("userId", ASCENDING), ("sessionId", ASCENDING)], unique=True)
    db.bookings.create_index([("sessionId", ASCENDING), ("status", ASCENDING)])
    db.conversations.create_index([("memberId", ASCENDING), ("trainerId", ASCENDING)], unique=True)
    db.messages.create_index([("conversationId", ASCENDING), ("_id", ASCENDING)])
    db.reviews.create_index("bookingId", unique=True)
    db.events.create_index([("publishedAt", ASCENDING), ("occurredAt", ASCENDING)])

def transaction(callback):
    from pymongo.read_concern import ReadConcern
    from pymongo.write_concern import WriteConcern
    with mongo_client.start_session() as session:
        return session.with_transaction(callback, read_concern=ReadConcern("snapshot"), write_concern=WriteConcern("majority"))
