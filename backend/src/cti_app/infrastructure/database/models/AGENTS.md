# ORM Models Package

Database ORM model organization by bounded context.

## Modules

| Module | Purpose | Key Tables |
|--------|---------|-----------|
| **base.py** | Declarative Base definition | `Base` SQLAlchemy registry |
| **classification.py** | TLP classification contract | SQL CHECK constraints for classification enums |
| **source_relationships.py** | Source relationship contract | SQL CHECK constraints for relationship types |
| **core.py** | Core domain entities | Blobs, Subjects, Documents, Samples, Provenance |
| **editions.py** | Versioned editions & audit | Edition, AuditLog |
| **discovery.py** | Discovery batch processing | DiscoveryBatch, CumulativeIdentity, Snapshots |
| **editorial.py** | Editorial decisions | EditorialGroups, HumanDecisions |
| **collection.py** | Collections & evidence | Collection, Artifacts, Claims, Indicators |
| **model_execution.py** | Model lifecycle | ModelRuns, Conversations, Execution lifecycle |
| **briefs.py** | Evidence packs | EvidencePacks, Drafts |
| **production.py** | Production runs | ProductionRuns, Artifacts, Batches |
| **jobs.py** | Background jobs | Jobs, Events |

## Navigation Rules

1. **Direct imports**: Import Row classes directly from their owner module
   ```python
   from cti_app.infrastructure.database.models.core import BlobRow, SubjectRow
   from cti_app.infrastructure.database.models.jobs import JobRow
   ```

2. **No re-exports**: `__init__.py` remains empty (docstring only)

3. **No dynamic registries**: All table registration is explicit and static

4. **New tables**: Add to owner module + explicit Alembic import

5. **Metadata integrity**: `Base.metadata` is authoritative and exhaustive

6. **Migration rules**:
   - SQL schema changes require migration
   - Table moves between files do not

7. **Forbidden**: Never recreate `schema.py` or `_shared.py`

## Verification

```bash
# No schema.py
test ! -e src/cti_app/infrastructure/database/models/schema.py

# No models.schema references
rg -n 'models\.schema' src tests migrations

# All metadata tables registered (migration test is the authoritative oracle)
cd backend && TEST_POSTGRES_ADMIN_DSN=postgres://user:pass@localhost/db uv run pytest tests/integration/test_migrations.py -q
```
