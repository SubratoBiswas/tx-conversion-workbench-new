"""Per-project connection to a source ERP system.

A ``SourceConnection`` represents how the workbench reaches a customer's
source ERP (NetSuite, EBS, ...) to run Discovery scans and, later, pull live
data extracts. Credentials live in ``credentials_encrypted`` as a Fernet
ciphertext — never plaintext, never logged.

Production-grade notes:

* Credentials are encrypted with the project's master key
  (``EncryptionService``) using a per-credential nonce. Rotating the master
  key requires re-encrypting every row (a maintenance script, not in scope
  for v1 but designed for).
* Connections are *scoped to a project* — a user with access to project A
  cannot read project B's credentials. RBAC checks live in the router.
* Every create / test / delete is written to the audit log so a compliance
  reviewer can prove who connected to what and when.
* ``mock_mode = True`` is the v1 default — the connection record exists but
  the scanner reads from a deterministic fixture set instead of hitting the
  real ERP. Customers flip mock_mode off once their read-only test instance
  is plumbed in.
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship

from app.database import Base


AUTH_TYPES = (
    "oauth1_tba",                # NetSuite Token-Based Authentication
    "oauth2_client_credentials", # NetSuite OAuth 2.0 / Workday / Salesforce
    "db_basic",                  # EBS Oracle DB user / password
    "db_wallet",                 # EBS Oracle Wallet
    "mock",                      # development / fixture-driven
)

CONNECTION_STATUSES = (
    "draft",      # created but never tested
    "ok",         # last test succeeded
    "degraded",   # last test partially succeeded (e.g., one endpoint of two)
    "failed",     # last test failed
)


class SourceConnection(Base):
    __tablename__ = "source_connections"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(
        Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Canonical source-system code (see app/source_systems.py)
    source_system = Column(String(50), nullable=False, index=True)

    # Human label visible in the UI ("Vertex NetSuite PROD", "EBS UAT").
    display_name = Column(String(255), nullable=False)

    # The reachable endpoint. NetSuite: SuiteTalk REST base URL. EBS: DB DSN.
    endpoint = Column(String(500), nullable=True)

    auth_type = Column(String(50), nullable=False, default="mock")
    # Non-secret connection metadata (account id, environment, schema name,
    # subsidiary list, etc.). Safe to display; never holds tokens or passwords.
    connection_metadata = Column(JSON, default=dict)

    # Fernet-encrypted credentials blob. Plaintext is a JSON object whose
    # shape depends on auth_type:
    #   oauth1_tba  -> { account_id, consumer_key, consumer_secret, token_id, token_secret }
    #   db_basic    -> { host, port, service_name, username, password }
    # The plaintext never leaves the encryption service.
    credentials_encrypted = Column(Text, nullable=True)
    # Sentinel that the row was saved with creds (so the UI can show
    # "Credentials configured" without us decrypting).
    has_credentials = Column(Boolean, default=False)

    # Default for v1: every new connection starts in mock mode unless the
    # caller explicitly opts into live. Discovery scanners read this flag.
    mock_mode = Column(Boolean, default=True, nullable=False)

    status = Column(String(50), default="draft", nullable=False)
    last_test_at = Column(DateTime, nullable=True)
    # Last-test success/failure detail: { latency_ms, version, message, probes: [...] }
    last_test_details = Column(JSON, nullable=True)

    created_by = Column(String(150), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship("Project")
