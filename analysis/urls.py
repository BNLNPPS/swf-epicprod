from django.urls import path

from . import api_views

app_name = 'analysis'

urlpatterns = [
    path('api/v1/dataset-card/', api_views.dataset_card, name='dataset_card'),
    path('api/v1/runs/<str:run_card_id>/', api_views.run_detail, name='run_detail'),
    path('api/v1/runs/<str:run_card_id>/job-outputs/', api_views.job_outputs_receive,
         name='job_outputs_receive'),
    path('api/v1/runs/<str:run_card_id>/result/', api_views.result_receive,
         name='result_receive'),
]
