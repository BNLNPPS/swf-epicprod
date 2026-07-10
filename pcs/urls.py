from django.urls import path, include
from . import views

app_name = 'pcs'

urlpatterns = [
    # Hub
    path('', views.pcs_hub, name='pcs_hub'),

    # Catalog (campaign-aware production task catalog)
    path('catalog/', views.pcs_catalog, name='pcs_catalog'),
    path('catalog/csv-update/', views.pcs_catalog_csv_update, name='pcs_catalog_csv_update'),
    path('catalog/past-update/', views.pcs_catalog_past_update, name='pcs_catalog_past_update'),
    path('catalog/rucio-update/', views.pcs_catalog_rucio_update, name='pcs_catalog_rucio_update'),
    path('catalog/evgen-update/', views.pcs_catalog_evgen_update, name='pcs_catalog_evgen_update'),
    path('catalog/questionnaire-match-update/', views.pcs_catalog_questionnaire_match_update, name='pcs_catalog_questionnaire_match_update'),
    path('catalog/progress-refresh/', views.pcs_catalog_progress_refresh, name='pcs_catalog_progress_refresh'),
    path('catalog/cache-refresh/', views.pcs_catalog_cache_refresh, name='pcs_catalog_cache_refresh'),
    path('catalog/set-current/', views.pcs_catalog_set_current, name='pcs_catalog_set_current'),
    path('catalog/promote-current/', views.pcs_catalog_promote_current, name='pcs_catalog_promote_current'),
    path('catalog/instancing-execute/', views.pcs_catalog_instancing_execute, name='pcs_catalog_instancing_execute'),
    path('physics/', views.pcs_physics_configs, name='pcs_physics_configs'),
    path('data/<str:name>/', views.pcs_edition_data, name='pcs_edition_data'),
    path('request/', views.pcs_request_composer, name='pcs_request_composer'),
    path('catalog/set-last/', views.pcs_catalog_set_last, name='pcs_catalog_set_last'),

    # Questionnaire intake
    path('questionnaires/', views.questionnaires_list, name='questionnaires_list'),
    path('questionnaires/import/', views.questionnaire_import, name='questionnaire_import'),
    path('questionnaires/<int:pk>/', views.questionnaire_detail, name='questionnaire_detail'),
    path('questionnaires/<int:pk>/matches/', views.questionnaire_match_add, name='questionnaire_match_add'),
    path('questionnaires/<int:pk>/matches/<int:task_id>/remove/', views.questionnaire_match_remove, name='questionnaire_match_remove'),

    # Rucio DID detail — self-hosted live browser (no public Rucio webui).
    # files route first: <path:name> is greedy, so the /files/ suffix must match before the page route.
    path('rucio/<str:scope>/<path:name>/files/', views.rucio_did_files, name='rucio_did_files'),
    path('rucio/<str:scope>/<path:name>/', views.rucio_did_detail, name='rucio_did_detail'),

    # Physics Categories
    path('categories/', views.physics_categories_list, name='physics_categories_list'),
    path('categories/create/', views.physics_category_create, name='physics_category_create'),

    # Tag compose — 2-panel browse + create (before generic list routes)
    path('tags/<str:tag_type>/compose/', views.tag_compose, name='tag_compose'),
    path('tags/<str:tag_type>/param-defs/', views.param_defs_api, name='param_defs_api'),
    path('tags/<str:tag_type>/<int:tag_number>/delete/', views.tag_delete, name='tag_delete'),

    # Tags (parameterized by type)
    path('tags/<str:tag_type>/', views.tags_list, name='tags_list'),
    path('tags/<str:tag_type>/datatable/', views.tags_datatable_ajax, name='tags_datatable_ajax'),
    path('tags/<str:tag_type>/<int:tag_number>/datasets/', views.tag_datasets, name='tag_datasets'),
    path('tags/<str:tag_type>/<int:tag_number>/', views.tag_detail, name='tag_detail'),
    path('tags/<str:tag_type>/<int:tag_number>/edit/', views.tag_edit, name='tag_edit'),
    path('tags/<str:tag_type>/<int:tag_number>/lock/', views.tag_lock, name='tag_lock'),

    # Datasets
    path('datasets/compose/', views.datasets_compose, name='datasets_compose'),
    path('datasets/', views.datasets_list, name='datasets_list'),
    path('datasets/datatable/', views.datasets_datatable_ajax, name='datasets_datatable_ajax'),
    path('datasets/create/', views.dataset_create, name='dataset_create'),
    path('datasets/<int:pk>/', views.dataset_detail, name='dataset_detail'),
    path('datasets/<int:pk>/add-block/', views.dataset_add_block, name='dataset_add_block'),
    # On-demand compose hydration (tag parameters) — light payload, fetched on open
    path('datasets/<int:pk>/compose-detail/', views.prod_task_compose_dataset_detail, name='compose_dataset_detail'),

    # Production Configs
    path('configs/compose/', views.prod_configs_compose, name='prod_configs_compose'),
    path('configs/', views.prod_configs_list, name='prod_configs_list'),
    path('configs/datatable/', views.prod_configs_datatable_ajax, name='prod_configs_datatable_ajax'),
    path('configs/create/', views.prod_config_create, name='prod_config_create'),
    path('configs/<int:pk>/', views.prod_config_detail, name='prod_config_detail'),
    path('configs/<int:pk>/edit/', views.prod_config_edit, name='prod_config_edit'),

    # Production Tasks
    path('tasks/', views.prod_tasks_list, name='prod_tasks_list'),
    path('tasks/datatable/', views.prod_tasks_datatable_ajax, name='prod_tasks_datatable_ajax'),
    path('tasks/compose/', views.prod_task_compose, name='prod_task_compose'),
    # Task routes are keyed by the composed tag name (str, no slashes); the
    # literal routes above are matched first. A stale /tasks/<pk>/ link still
    # resolves (resolve_prodtask tolerates a bare pk) and the detail view 301s
    # it to the composed-name URL. No task URL ever emits a pk.
    path('tasks/<str:name>/', views.prod_task_detail, name='prod_task_detail'),
    path('tasks/<str:name>/delete/', views.prod_task_delete, name='prod_task_delete'),
    path('tasks/<str:name>/commands/', views.prod_task_generate_commands, name='prod_task_generate_commands'),
    # On-demand compose hydration (taskParamMap + commands) — light payload, fetched on open
    path('tasks/<str:name>/compose-detail/', views.prod_task_compose_task_detail, name='compose_task_detail'),

    # REST API
    path('api/', include('pcs.api_urls')),
]
