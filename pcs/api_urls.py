from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .api_views import (
    PhysicsCategoryViewSet, PhysicsTagViewSet,
    EvgenTagViewSet, SimuTagViewSet, RecoTagViewSet, BackgroundTagViewSet,
    DatasetViewSet, ProdConfigViewSet, ProdTaskViewSet, QuestionnaireViewSet,
    prod_request_compose, campaigns_status, physics_configs_requestors,
    validation_v1_index,
    validation_sample_completion, validation_campaign_completion,
    validation_campaign_catalog, validation_results_receive,
    evgen_mark, evgen_register, pc_ingest_analyze, pc_ingest_accept,
)

router = DefaultRouter()
router.register(r'physics-categories', PhysicsCategoryViewSet, basename='physics-category')
router.register(r'physics-tags', PhysicsTagViewSet, basename='physics-tag')
router.register(r'evgen-tags', EvgenTagViewSet, basename='evgen-tag')
router.register(r'simu-tags', SimuTagViewSet, basename='simu-tag')
router.register(r'reco-tags', RecoTagViewSet, basename='reco-tag')
router.register(r'background-tags', BackgroundTagViewSet, basename='background-tag')
router.register(r'datasets', DatasetViewSet, basename='dataset')
router.register(r'questionnaires', QuestionnaireViewSet, basename='questionnaire')
router.register(r'prod-configs', ProdConfigViewSet, basename='prod-config')
router.register(r'prod-tasks', ProdTaskViewSet, basename='prod-task')

urlpatterns = [
    # Validation interface v1 (EPICPROD_VALIDATION.md § REST interface)
    path('v1/', validation_v1_index, name='validation_v1_index'),
    path('v1/samples/<str:sample>/completion/', validation_sample_completion,
         name='validation_sample_completion'),
    path('v1/campaigns/<str:campaign>/completion/',
         validation_campaign_completion,
         name='validation_campaign_completion'),
    path('v1/campaigns/<str:campaign>/catalog/', validation_campaign_catalog,
         name='validation_campaign_catalog'),
    path('v1/validation-results/', validation_results_receive,
         name='validation_results'),
    path('prod-requests/compose/', prod_request_compose,
         name='prod_request_compose'),
    path('campaigns/status/', campaigns_status, name='campaigns_status'),
    path('physics-configs/requestors/', physics_configs_requestors,
         name='physics_configs_requestors'),
    path('evgen/marks/', evgen_mark, name='evgen_mark'),
    path('evgen/register/', evgen_register, name='evgen_register'),
    path('ingest/analyze/', pc_ingest_analyze, name='pc_ingest_analyze'),
    path('ingest/accept/', pc_ingest_accept, name='pc_ingest_accept'),
    path('', include(router.urls)),
]
