"""Analysis REST interface (docs/ANALYSIS.md, REST).

Reads are public, like the PCS catalog: a dataset card or a run card is a
statement of record that anyone reproducing a result needs. Writes come
from the operations agent's ingest of job reports, with its token.
"""
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import (api_view, authentication_classes,
                                       permission_classes)
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from monitor_app.middleware import TunnelAuthentication

from . import cards, services
from .models import AnalysisRun, DatasetCard

WRITE_AUTH = [TunnelAuthentication, SessionAuthentication, TokenAuthentication]


def _card(sample, revision):
    qs = DatasetCard.objects.filter(sample=sample.strip('/'))
    if revision:
        return get_object_or_404(qs, revision=revision)
    card = qs.order_by('-revision').first()
    if card is None:
        from django.http import Http404
        raise Http404(f'no dataset card for {sample}')
    return card


@api_view(['GET'])
@permission_classes([AllowAny])
def dataset_card(request):
    """GET ?sample=RECO/...&revision=N (latest when omitted): the card's
    header. Add &manifest=1 for the manifest itself, gzipped CSV."""
    sample = request.GET.get('sample') or ''
    if not sample:
        return Response({'detail': 'give ?sample=RECO/<version>/<config>/...'},
                        status=status.HTTP_400_BAD_REQUEST)
    card = _card(sample, request.GET.get('revision'))
    if request.GET.get('manifest'):
        response = HttpResponse(bytes(card.manifest), content_type='application/gzip')
        response['Content-Disposition'] = (
            f'attachment; filename="manifest.r{card.revision}.csv.gz"')
        return response
    return Response(dict(card.header, dataset=card.dataset_id,
                         frozen_by=card.frozen_by))


@api_view(['GET'])
@permission_classes([AllowAny])
def run_detail(request, run_card_id):
    """A frozen run: its run card, status, PanDA tasks and result."""
    run = get_object_or_404(AnalysisRun, run_card_id=run_card_id)
    return Response({
        'run_card_id': run.run_card_id, 'tag': run.tag.label, 'status': run.status,
        'purpose': run.purpose, 'owner': run.owner, 'campaign': run.campaign,
        'run_card': run.run_card, 'panda_tasks': run.panda_tasks,
        'job_outputs': run.job_outputs.count(), 'result': run.result,
    })


@api_view(['POST'])
@authentication_classes(WRITE_AUTH)
@permission_classes([IsAuthenticated])
def job_outputs_receive(request, run_card_id):
    """Record map jobs' partials. Body: {"outputs": [{"seq", "pandaid",
    "did", "adler32", "bytes", "files_read", "events_read", "status",
    "reason", "reported_at"}]}. Idempotent per (run, seq)."""
    run = get_object_or_404(AnalysisRun, run_card_id=run_card_id)
    outputs = (request.data or {}).get('outputs') if isinstance(request.data, dict) else None
    if not isinstance(outputs, list) or not outputs:
        return Response({'detail': 'body must be {"outputs": [{"seq", "did", ...}]}'},
                        status=status.HTTP_400_BAD_REQUEST)
    written, refused = services.record_job_outputs(run, outputs)
    return Response({'written': written, 'refused': refused})


@api_view(['POST'])
@authentication_classes(WRITE_AUTH)
@permission_classes([IsAuthenticated])
def result_receive(request, run_card_id):
    """Record the result card one sample's merge job published."""
    run = get_object_or_404(AnalysisRun, run_card_id=run_card_id)
    result = request.data if isinstance(request.data, dict) else {}
    if result.get('run_id') != run.run_card_id or not result.get('sample'):
        return Response({'detail': 'a result card of this run, with its sample'},
                        status=status.HTTP_400_BAD_REQUEST)
    services.record_result(run, result)
    return Response({'status': run.status})
