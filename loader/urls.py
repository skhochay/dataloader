from pathlib import Path

from django.urls import path
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.generic import TemplateView


class IndexView(TemplateView):
    """The page that loads the built bundle. The bundle's file name never changes, so the file's date is added to its URL:
    after `npm run build` the browser fetches the new bundle instead of reusing a cached old one."""

    template_name = "index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        folder = Path(__file__).parent / "static" / "app"
        context["build"] = max((int(p.stat().st_mtime) for p in (folder / "app.js", folder / "app.css") if p.exists()), default=0)
        return context

from . import views, views_databases, views_directories, views_mappings

urlpatterns = [
    path("", ensure_csrf_cookie(IndexView.as_view()), name="index"),
    path("api/stats/", views.stats),
    path("api/jobs/", views.jobs),
    path("api/jobs/<int:pk>/", views.job_detail),
    path("api/jobs/<int:pk>/retry/", views.retry),
    path("api/directories/", views_directories.collection),
    path("api/directories/test/", views_directories.test),
    path("api/directories/<int:pk>/", views_directories.item),
    path("api/directories/<int:pk>/files/<str:name>/", views_directories.files),
    path("api/directories/<int:pk>/upload/", views_directories.upload),
    path("api/engine/start/", views.engine_start),
    path("api/engine/stop/", views.engine_stop),
    path("api/databases/", views_databases.collection),
    path("api/databases/test/", views_databases.test),
    path("api/databases/<int:pk>/", views_databases.item),
    path("api/mappings/", views_mappings.collection),
    path("api/mappings/match/", views_mappings.match),
    path("api/mappings/tables/", views_mappings.tables),
    path("api/mappings/verify/", views_mappings.verify),
    path("api/mappings/<int:pk>/", views_mappings.item),
    path("api/config/", views.config),
    path("api/config/reset/", views.config_reset),
]
