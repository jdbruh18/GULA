from sqlalchemy import create_engine, Column, String, Date, DateTime, JSON, ForeignKey, func
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

class Patient(Base):
    __tablename__ = 'patients'
    
    id = Column(String(255), primary_key=True)
    first_name = Column(String(255), nullable=False)
    last_name = Column(String(255), nullable=False)
    gender = Column(String(50), nullable=False)
    birth_date = Column(Date, nullable=False)
    tenant_id = Column(String(100), nullable=False)
    created_at = Column(DateTime, server_default=func.now())

class PatientTimelineEvent(Base):
    __tablename__ = 'patient_timeline_events'
    
    id = Column(String(255), primary_key=True) # event_id from envelope
    patient_id = Column(String(255), nullable=False, index=True)
    event_type = Column(String(100), nullable=False)
    source = Column(String(100), nullable=False)
    timestamp = Column(DateTime, nullable=False)
    payload = Column(JSON, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

def init_db(database_url):
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session
