from typing import Tuple

from sqlalchemy.orm import Query, Session

from balsam import schemas
from balsam.server import models
from balsam.server.routers.filters import EventQuery
from balsam.server.util import Paginator


def fetch(
    db: Session, owner: schemas.UserOut, paginator: Paginator[models.LogEvent], filterset: EventQuery
) -> "Tuple[int, Query[models.LogEvent]]":
    qs = db.query(models.LogEvent).join(models.Job).join(models.App).join(models.Site)  # type: ignore
    qs = qs.filter(models.Site.owner_id == owner.id)
    qs = filterset.apply_filters(qs)
    count = qs.group_by(models.LogEvent.id).count()
    events = paginator.paginate(qs)
    print(*((e.from_state, e.to_state) for e in events), sep="\n")
    return count, events
