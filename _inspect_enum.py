from fpl_intelligence.db.base import Base
import fpl_intelligence.availability.models as m  # noqa: F401

for tname in ['availability_sources', 'availability_evidence', 'availability_events', 'player_mentions']:
    t = Base.metadata.tables[tname]
    for c in t.columns:
        ctype = c.type
        if hasattr(ctype, 'enums'):
            print(f"{tname}.{c.name}: {type(ctype).__name__} name={getattr(ctype,'name',None)} enums={ctype.enums}")
