from django.contrib import admin

from .models import DatabaseConnection, DirectoryProfile, EngineSetting, LoadJob, Record


@admin.register(LoadJob)
class LoadJobAdmin(admin.ModelAdmin):
    list_display = ("filename", "profile_name", "status", "rows_loaded", "started_at", "finished_at")
    list_filter = ("status",)
    search_fields = ("filename", "checksum")
    readonly_fields = [f.name for f in LoadJob._meta.fields]


@admin.register(Record)
class RecordAdmin(admin.ModelAdmin):
    list_display = ("job", "row_number")
    raw_id_fields = ("job",)


@admin.register(EngineSetting)
class EngineSettingAdmin(admin.ModelAdmin):
    list_display = ("key", "value", "updated_at")


@admin.register(DatabaseConnection)
class DatabaseConnectionAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "host", "database", "username")
    exclude = ("password_enc",)  # never shown, even encrypted


@admin.register(DirectoryProfile)
class DirectoryProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "enabled", "stage_dir")
