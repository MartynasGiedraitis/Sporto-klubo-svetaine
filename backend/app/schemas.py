from datetime import datetime
from typing import Optional, Literal
from pydantic import BaseModel, Field, ConfigDict, EmailStr, AwareDatetime

class Input(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

class Registration(Input):
    firstName: str = Field(min_length=1, max_length=80)
    lastName: str = Field(min_length=1, max_length=80)
    birthYear: int = Field(ge=1900, le=datetime.now().year)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

class Login(Input):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

class Plan(Input):
    name: str = Field(min_length=1, max_length=200)
    price: float = Field(ge=0, le=100000)
    durationDays: int = Field(ge=1, le=3650)
    description: str = Field(default="", max_length=2000)
    isActive: bool = True

class RoomInput(Input):
    name: str = Field(min_length=1, max_length=200)
    capacity: int = Field(ge=1, le=10000)
    location: str = Field(default="", max_length=500)
    isActive: bool = True

class ExerciseInput(Input):
    name: str = Field(min_length=1, max_length=200)
    muscleGroup: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    isActive: bool = True

class TrainerInput(Registration):
    specialization: str = Field(default="", max_length=500)
    isActive: bool = True

class TrainerPatch(Input):
    firstName: Optional[str] = Field(default=None, min_length=1, max_length=80)
    lastName: Optional[str] = Field(default=None, min_length=1, max_length=80)
    birthYear: Optional[int] = Field(default=None, ge=1900, le=datetime.now().year)
    email: Optional[EmailStr] = None
    specialization: Optional[str] = Field(default=None, max_length=500)
    isActive: Optional[bool] = None

class MembershipPurchase(Input):
    planId: str

class SessionInput(Input):
    trainerId: Optional[str] = None
    roomId: str
    title: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=4000)
    startsAt: AwareDatetime
    endsAt: AwareDatetime
    capacity: int = Field(ge=1, le=10000)

class BookingInput(Input):
    sessionId: str

class AttendanceInput(Input):
    status: Literal["attended", "no_show"]

class ReviewInput(Input):
    bookingId: str
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=2000)

class LogInput(Input):
    exerciseId: str
    performedAt: AwareDatetime
    sets: Optional[int] = Field(default=None, ge=1, le=100)
    repetitions: Optional[int] = Field(default=None, ge=1, le=10000)
    weight: Optional[float] = Field(default=None, ge=0, le=2000)
    durationSeconds: Optional[int] = Field(default=None, ge=1, le=86400)
    notes: str = Field(default="", max_length=2000)

class ConversationInput(Input):
    trainerId: str

class MessageInput(Input):
    content: str = Field(min_length=1, max_length=4000)
