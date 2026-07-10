"""
PCS REST API ViewSets.

Endpoints under /pcs/api/. All endpoints require authentication.
Tag immutability enforced: PATCH returns 400 on locked tags. Lock is one-way via POST /lock/.
Tag delete via POST /delete/ — creator-only, draft-only (locked tags protected by PROTECT FK).
Tag numbers auto-assigned on POST: physics from category range, e/s/r from PersistentState.
Dataset creation requires all four tags to be locked. created_by set from authenticated user.
"""
from rest_framework import viewsets, status
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from monitor_app.middleware import TunnelAuthentication
from rest_framework.decorators import (action, api_view,
    authentication_classes, permission_classes)
from rest_framework.permissions import (IsAuthenticated,
    IsAuthenticatedOrReadOnly, SAFE_METHODS, BasePermission)


class IsOwnerOrReadOnly(BasePermission):
    """Read open to anyone; write requires authenticated owner (by created_by username)."""
    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        return getattr(obj, 'created_by', None) == request.user.username
from rest_framework.response import Response
from django.db.models import Count
from monitor_app.models import UserPreference

from .models import (
    PhysicsCategory, PhysicsTag, EvgenTag, SimuTag, RecoTag, BackgroundTag,
    Dataset, ProdConfig, ProdTask, PandaTasks, Questionnaire,
)
from .serializers import (
    PhysicsCategorySerializer, PhysicsTagSerializer,
    EvgenTagSerializer, SimuTagSerializer, RecoTagSerializer, BackgroundTagSerializer,
    DatasetSerializer, ProdConfigSerializer, ProdTaskSerializer,
    QuestionnaireSerializer,
)
from .schemas import validate_parameters, get_tag_model
from . import services
from monitor_app.epicprod_logging import log_epicprod_action
from .services import ServiceError


class PhysicsCategoryViewSet(viewsets.ModelViewSet):
    """CRUD for physics categories. Categories are mutable (no lock lifecycle)."""
    queryset = PhysicsCategory.objects.annotate(tag_count=Count('tags'))
    serializer_class = PhysicsCategorySerializer
    authentication_classes = [TunnelAuthentication, SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticatedOrReadOnly]
    http_method_names = ['get', 'post', 'patch', 'head', 'options']

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user.username)


class _TagViewSetMixin:
    """Shared behavior for all tag ViewSets: draft/locked lifecycle, PATCH guard, lock/delete actions."""
    authentication_classes = [TunnelAuthentication, SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticatedOrReadOnly]
    http_method_names = ['get', 'post', 'patch', 'head', 'options']
    lookup_field = 'tag_number'

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.status == 'locked':
            return Response(
                {'detail': f'Tag {instance.tag_label} is locked and cannot be modified.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if 'status' in request.data:
            return Response(
                {'detail': 'Use the /lock/ endpoint to change status.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().partial_update(request, *args, **kwargs)

    @action(detail=True, methods=['post'])
    def lock(self, request, **kwargs):
        instance = self.get_object()
        if instance.status == 'locked':
            return Response(
                {'detail': f'Tag {instance.tag_label} is already locked.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        instance.status = 'locked'
        instance.save(update_fields=['status', 'updated_at'])
        return Response(self.get_serializer(instance).data)

    @action(detail=True, methods=['post'], url_path='delete')
    def soft_delete(self, request, **kwargs):
        instance = self.get_object()
        if instance.status == 'locked':
            return Response(
                {'detail': f'Tag {instance.tag_label} is locked and cannot be deleted.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if instance.created_by != request.user.username:
            return Response(
                {'detail': f'Only the creator ({instance.created_by}) can delete this tag.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        label = instance.tag_label
        instance.delete()
        return Response({'detail': f'Tag {label} deleted.'})


class PhysicsTagViewSet(_TagViewSetMixin, viewsets.ModelViewSet):
    queryset = PhysicsTag.objects.select_related('category')
    serializer_class = PhysicsTagSerializer

    def create(self, request, *args, **kwargs):
        category_digit = request.data.get('category')
        if not category_digit:
            return Response(
                {'category': ['This field is required.']},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            category = PhysicsCategory.objects.get(digit=category_digit)
        except PhysicsCategory.DoesNotExist:
            return Response(
                {'category': [f'Category {category_digit} does not exist.']},
                status=status.HTTP_400_BAD_REQUEST,
            )
        tag_number = PhysicsTag.allocate_next(category)
        data = request.data.copy()
        data['tag_number'] = tag_number
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        serializer.save(tag_number=tag_number, tag_label=f"p{tag_number}",
                        created_by=request.user.username)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class _SimpleTagViewSet(_TagViewSetMixin, viewsets.ModelViewSet):
    tag_type = None

    def create(self, request, *args, **kwargs):
        model = get_tag_model(self.tag_type)
        tag_number = model.allocate_next()
        prefix = self.tag_type
        data = request.data.copy()
        data['tag_number'] = tag_number
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        serializer.save(tag_number=tag_number, tag_label=f"{prefix}{tag_number}",
                        created_by=request.user.username)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class EvgenTagViewSet(_SimpleTagViewSet):
    queryset = EvgenTag.objects.all()
    serializer_class = EvgenTagSerializer
    tag_type = 'e'


class SimuTagViewSet(_SimpleTagViewSet):
    queryset = SimuTag.objects.all()
    serializer_class = SimuTagSerializer
    tag_type = 's'


class RecoTagViewSet(_SimpleTagViewSet):
    queryset = RecoTag.objects.all()
    serializer_class = RecoTagSerializer
    tag_type = 'r'


class BackgroundTagViewSet(_SimpleTagViewSet):
    queryset = BackgroundTag.objects.all()
    serializer_class = BackgroundTagSerializer
    tag_type = 'k'


class DatasetViewSet(viewsets.ModelViewSet):
    """Dataset CRUD. POST composes tags and creates block 1. No DELETE.

    Tags may be draft during alpha — reproducibility locking is enforced at
    submission prep, not composition. See docs/COMMISSIONING_RELAXATIONS.md."""
    queryset = Dataset.objects.select_related(
        'physics_tag', 'evgen_tag', 'simu_tag', 'reco_tag', 'background_tag'
    )
    serializer_class = DatasetSerializer
    authentication_classes = [TunnelAuthentication, SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticatedOrReadOnly]
    http_method_names = ['get', 'post', 'head', 'options']

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        instance = serializer.save(created_by=request.user.username)
        return Response(self.get_serializer(instance).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'], url_path='intake')
    def intake(self, request):
        """
        Idempotent intake of an external (e.g. EVGEN CSV manifest) Dataset.

        Thin wrapper over ``services.dataset_intake``; see that for the
        full contract.
        """
        try:
            ds, created = services.dataset_intake(
                source_location=request.data.get('source_location'),
                source_kind=request.data.get('source_kind', 'csv_manifest'),
                scope=request.data.get('scope', 'group.EIC.evgen'),
                stage=request.data.get('stage', 'evgen'),
                detector_version=request.data.get('detector_version'),
                detector_config=request.data.get('detector_config'),
                physics_tag_label=request.data.get('physics_tag'),
                evgen_tag_label=request.data.get('evgen_tag'),
                simu_tag_label=request.data.get('simu_tag'),
                reco_tag_label=request.data.get('reco_tag'),
                background_tag_label=request.data.get('background_tag'),
                description=request.data.get('description', ''),
                created_by=request.user.username,
            )
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        http_status = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(self.get_serializer(ds).data, status=http_status)

    @action(detail=True, methods=['post'], url_path='add-block')
    def add_block(self, request, pk=None):
        dataset = self.get_object()
        new_block_num = dataset.blocks + 1
        # Update blocks count on all rows with this dataset_name
        Dataset.objects.filter(dataset_name=dataset.dataset_name).update(blocks=new_block_num)
        # Create the new block
        new_block = Dataset.objects.create(
            dataset_name=dataset.dataset_name,
            scope=dataset.scope,
            detector_version=dataset.detector_version,
            detector_config=dataset.detector_config,
            physics_tag=dataset.physics_tag,
            evgen_tag=dataset.evgen_tag,
            simu_tag=dataset.simu_tag,
            reco_tag=dataset.reco_tag,
            background_tag=dataset.background_tag,
            block_num=new_block_num,
            blocks=new_block_num,
            did=f"{dataset.scope}:{dataset.dataset_name}.b{new_block_num}",
            description=dataset.description,
            metadata=dataset.metadata,
            created_by=request.user.username,
        )
        return Response(self.get_serializer(new_block).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'], url_path='propagation')
    def propagation(self, request):
        """Single or bulk propagation flip with required comment.

        Body: ``names`` (list of composed names), ``state``
        (continue|hold|final), ``comment`` (required), ``replaced_by``
        (optional), ``filter`` (optional — the selecting filter querystring,
        recorded for audit). Thin wrapper over
        ``services.dataset_propagation_set``; one action-stream event per
        call.
        """
        try:
            result = services.dataset_propagation_set(
                request.data.get('names') or [],
                request.data.get('state'),
                request.data.get('comment'),
                replaced_by=request.data.get('replaced_by', ''),
                changed_by=request.user.username,
                filter_state=request.data.get('filter', ''),
            )
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        return Response(result, status=status.HTTP_200_OK)


class ProdConfigViewSet(viewsets.ModelViewSet):
    """Production configuration templates. Owner-only edit; anyone can create."""
    queryset = ProdConfig.objects.all()
    serializer_class = ProdConfigSerializer
    authentication_classes = [TunnelAuthentication, SessionAuthentication, TokenAuthentication]
    permission_classes = [IsOwnerOrReadOnly]

    def perform_create(self, serializer):
        obj = serializer.save(created_by=self.request.user.username)
        _record_prod_config_scout_pref(self.request.user.username, obj)

    def perform_update(self, serializer):
        obj = serializer.save()
        _record_prod_config_scout_pref(self.request.user.username, obj)


def _record_prod_config_scout_pref(username, config):
    data = config.data or {}
    if isinstance(data, dict) and 'skip_scout' in data:
        UserPreference.set_pref(
            username,
            'prod_config_scout_mode',
            not bool(data.get('skip_scout')),
        )


class QuestionnaireViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only Google Form response mirror plus authenticated intake."""
    queryset = Questionnaire.objects.all()
    serializer_class = QuestionnaireSerializer
    authentication_classes = [TunnelAuthentication, SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticatedOrReadOnly]

    @action(detail=False, methods=['post'], url_path='intake')
    def intake(self, request):
        try:
            if 'csv_text' in request.data:
                summary = services.questionnaire_intake_csv(
                    request.data.get('csv_text'),
                    source_url=request.data.get('source_url', ''),
                    created_by=request.user.username,
                )
            else:
                rows = request.data.get('rows')
                if rows is None:
                    return Response(
                        {'detail': 'Provide either csv_text or rows.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                summary = services.questionnaire_intake(
                    rows,
                    source_url=request.data.get('source_url', ''),
                    created_by=request.user.username,
                )
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        return Response(summary)


class ProdTaskViewSet(viewsets.ModelViewSet):
    """Production tasks: Dataset + ProdConfig compositions with command generation."""
    queryset = ProdTask.objects.select_related(
        'dataset', 'dataset__physics_tag', 'dataset__evgen_tag',
        'dataset__simu_tag', 'dataset__reco_tag', 'prod_config',
    ).prefetch_related('panda_tasks')
    serializer_class = ProdTaskSerializer
    authentication_classes = [TunnelAuthentication, SessionAuthentication, TokenAuthentication]
    permission_classes = [IsAuthenticatedOrReadOnly]

    # Detail routes are keyed by the composed tag name. Composed names contain
    # dots, so the default lookup regex ([^/.]+) is widened to allow them.
    # get_object() resolves the composed name (and, inbound-only, the legacy
    # stored name or a bare pk) via the shared resolver, so /pcs/api/prod-tasks/
    # never emits a pk. The detail=False actions (command, record-submission, …)
    # are registered before this detail route, so they are matched first.
    lookup_field = 'name'
    lookup_value_regex = '[^/]+'

    def get_object(self):
        try:
            task = services.resolve_prodtask(
                self.kwargs[self.lookup_field], self.get_queryset())
        except ProdTask.DoesNotExist:
            from django.http import Http404
            raise Http404(f"No task {self.kwargs.get(self.lookup_field)!r}")
        self.check_object_permissions(self.request, task)
        return task

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user.username)

    @action(detail=True, methods=['post'], url_path='generate-commands')
    def generate_commands(self, request, name=None):
        task = self.get_object()
        task.generate_commands()
        task.save(update_fields=['condor_command', 'panda_command', 'updated_at'])
        return Response({
            'condor_command': task.condor_command,
            'panda_command': task.panda_command,
        })

    @action(detail=False, methods=['get'], url_path='command')
    def command(self, request):
        """
        Regenerate and return a task's submission artifact in one of three
        formats. Lookup by task name. No DB writes.

        Query params:
            name — ProdTask.name (required)
            fmt  — condor | panda | jedi | dump (required). Named 'fmt'
                   (not 'format') because DRF reserves 'format' for
                   content negotiation.

        Returns:
            text/plain for condor/panda, application/json for jedi/dump.
        """
        from django.http import HttpResponse, JsonResponse
        from .commands import (
            build_condor_command, build_panda_command,
            build_task_params, build_task_dump, build_evgen_task_params,
        )

        name = request.query_params.get('name')
        fmt = request.query_params.get('fmt', '').lower()
        if not name:
            return Response({'detail': 'Missing ?name='}, status=status.HTTP_400_BAD_REQUEST)
        if fmt not in ('condor', 'panda', 'jedi', 'evgen', 'dump'):
            return Response(
                {'detail': "fmt must be one of: condor, panda, jedi, evgen, dump"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            task = services.resolve_prodtask(name, self.get_queryset())
        except ProdTask.DoesNotExist:
            return Response({'detail': f"No task named '{name}'"}, status=status.HTTP_404_NOT_FOUND)

        if fmt == 'condor':
            return HttpResponse(build_condor_command(task), content_type='text/plain')
        if fmt == 'panda':
            return HttpResponse(build_panda_command(task), content_type='text/plain')
        if fmt == 'jedi':
            return JsonResponse(build_task_params(task), json_dumps_params={'indent': 2})
        if fmt == 'evgen':
            # Client-API EVGEN production spec for the submit-evgen-task doer.
            # Builds the manifest by resolving the matched DID(s) against JLab
            # Rucio. A misconfigured task raises ValueError (400); a Rucio
            # failure raises ServiceError (its status) — never a silent empty
            # spec.
            try:
                panda_tasks = None
                panda_tasks_id = request.query_params.get('panda_tasks_id')
                if panda_tasks_id:
                    panda_tasks = PandaTasks.objects.filter(
                        pk=panda_tasks_id, prod_task=task).first()
                    if panda_tasks is None:
                        return Response(
                            {'detail': f'No PandaTasks association {panda_tasks_id} for this task.'},
                            status=status.HTTP_404_NOT_FOUND,
                        )
                return JsonResponse(build_evgen_task_params(task, panda_tasks=panda_tasks),
                                    json_dumps_params={'indent': 2})
            except ValueError as e:
                return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
            except ServiceError as e:
                return Response({'detail': e.detail}, status=e.status)
        return JsonResponse(build_task_dump(task), json_dumps_params={'indent': 2})

    @action(detail=True, methods=['post'], url_path='set-status')
    def set_status(self, request, name=None):
        task = self.get_object()
        try:
            services.prodtask_set_status(
                task=task, new_status=request.data.get('status'),
            )
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        return Response(self.get_serializer(task).data)

    @action(detail=True, methods=['post'])
    def lock(self, request, name=None):
        """Lock a draft task → 'ready'. PCS-consistent with the tag lock: a
        dedicated, one-way lifecycle action — the UI offers no unlock, so a
        locked task is frozen for reproducibility. 'ready' is the task's locked
        state. Authenticated users may operate production tasks; the transition
        map (draft → ready only) is enforced by the service."""
        task = self.get_object()
        try:
            services.prodtask_set_status(task=task, new_status='ready')
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        return Response(self.get_serializer(task).data)

    @action(detail=True, methods=['post'])
    def submit(self, request, name=None):
        """Request automated PanDA submission of a locked (ready) task — the
        submit trigger used by the compose panel (and the task-detail "Submit in
        Compose" link), so the user can submit without leaving the view.
        Authenticated users may operate production tasks; the web tier holds no
        PanDA credential, so this only publishes a request to the prod-ops
        agent."""
        task = self.get_object()
        try:
            services.prodtask_submit_request(task=task)
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        data = dict(self.get_serializer(task).data)
        # Commissioning: submit is allowed from draft; readiness problems are
        # surfaced as a non-blocking warning rather than gating the submission.
        warnings = services.prodtask_readiness_problems(task)
        if warnings:
            data['warnings'] = warnings
        return Response(data)

    @action(detail=True, methods=['post'], url_path='panda-add-retry')
    def panda_add_retry(self, request, name=None):
        """Ask PanDA to increase allowed attempts for an existing JEDI task."""
        task = self.get_object()
        try:
            result = services.prodtask_panda_operation_request(
                task=task,
                operation='increase_attempts',
                jedi_task_id=request.data.get('jedi_task_id'),
                increase=request.data.get('increase', 1),
                created_by=getattr(request.user, 'username', '') or 'operator',
            )
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        return Response(result, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=['post'], url_path='panda-retry-failures')
    def panda_retry_failures(self, request, name=None):
        """Ask PanDA to retry failed work in an existing JEDI task."""
        task = self.get_object()
        try:
            result = services.prodtask_panda_operation_request(
                task=task,
                operation='retry_failures',
                jedi_task_id=request.data.get('jedi_task_id'),
                new_parameters=request.data.get('new_parameters') or {},
                created_by=getattr(request.user, 'username', '') or 'operator',
            )
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        return Response(result, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=['post'], url_path='rerun-entire-task')
    def rerun_entire_task(self, request, name=None):
        """Queue a new full PanDA task attempt for this campaign task."""
        task = self.get_object()
        try:
            services.prodtask_rerun_entire_task_request(task=task)
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        return Response(self.get_serializer(task).data, status=status.HTTP_202_ACCEPTED)

    @action(detail=False, methods=['post'], url_path='rucio-snapshot-update')
    def rucio_snapshot_update(self, request):
        """Request a JLab Rucio snapshot refresh for the current campaign — the
        external-safe trigger for the catalog 'Update from Rucio' button (a
        /pcs/api/ POST returning JSON, so it survives the swf-remote proxy; see
        docs/EPICPROD_OPS_AGENT.md). The web tier holds no credential; this only
        publishes a rucio_snapshot_update to the prod-ops agent, which refreshes
        the snapshot and rematches produced datasets onto each task's outputs in
        the background, then pushes rucio_snapshot_ready over the SSE relay."""
        user = getattr(request.user, 'username', '') or 'rucio_snapshot'
        try:
            services.rucio_snapshot_update_request(created_by=user)
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        return Response({'status': 'queued'}, status=status.HTTP_202_ACCEPTED)

    @action(detail=False, methods=['post'], url_path='evgen-rucio-update')
    def evgen_rucio_update(self, request):
        """Request a JLab Rucio EVGEN-input assimilation — the external-safe
        trigger for the catalog 'Update EVGEN from Rucio' button (a /pcs/api/
        POST returning JSON, so it survives the swf-remote proxy). The web tier
        holds no credential; this only publishes an evgen_rucio_update to the
        prod-ops agent, which fetches epic:/EVGEN/*, resolves each PCS evgen
        Dataset onto metadata['rucio'] in the background, then pushes
        evgen_rucio_ready over the SSE relay. See docs/EPICPROD_EVGEN_INPUTS.md."""
        user = getattr(request.user, 'username', '') or 'evgen_rucio'
        try:
            services.evgen_rucio_update_request(created_by=user)
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        return Response({'status': 'queued'}, status=status.HTTP_202_ACCEPTED)

    @action(detail=False, methods=['post'], url_path='questionnaire-match-update')
    def questionnaire_match_update(self, request):
        """Request a background rebuild of the task-local questionnaire-match
        cache. The web tier only queues questionnaire_match_update; the prod-ops
        agent writes ProdTask.overrides['questionnaire_matches'] and pushes
        questionnaire_match_ready over the SSE relay."""
        user = getattr(request.user, 'username', '') or 'questionnaire_match'
        try:
            services.questionnaire_match_update_request(created_by=user)
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        return Response({'status': 'queued'}, status=status.HTTP_202_ACCEPTED)

    @action(detail=False, methods=['post'], url_path='campaign-progress-refresh')
    def campaign_progress_refresh(self, request):
        """Request a background rebuild of the current campaign progress cache.

        The web tier only queues campaign_progress_refresh; the prod-ops agent
        rebuilds both the PanDA progress snapshot and rendered progress table
        cache, then pushes campaign_progress_ready over the SSE relay.
        """
        user = getattr(request.user, 'username', '') or 'progress_refresh'
        try:
            services.campaign_progress_refresh_request(created_by=user)
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        return Response({'status': 'queued'}, status=status.HTTP_202_ACCEPTED)

    @action(detail=False, methods=['post'], url_path='catalog-import')
    def catalog_import(self, request):
        """Request a background catalog import — the external-safe trigger for the
        'Update from CSV' and 'Update from epic-prod' buttons. The web tier only
        publishes a catalog_import to the prod-ops agent, which runs the import
        off the WSGI request (the epic-prod walk times the gateway out) and pushes
        catalog_import_ready over the SSE relay. ``source``: 'csv' | 'epic-prod'.
        See docs/EPICPROD_OPS_AGENT.md, docs/SSE_PUSH.md."""
        user = getattr(request.user, 'username', '') or 'catalog_import'
        try:
            services.catalog_import_request(request.data.get('source'), created_by=user)
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        return Response({'status': 'queued'}, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=['post'], url_path='link-input')
    def link_input(self, request, name=None):
        """Thin wrapper over ``services.prodtask_link_input``."""
        task = self.get_object()
        try:
            services.prodtask_link_input(
                task=task,
                did=request.data.get('did'),
                dids=request.data.get('dids'),
            )
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        return Response(self.get_serializer(task).data)

    @action(detail=False, methods=['post'], url_path='intake')
    def intake(self, request):
        """Thin wrapper over ``services.prodtask_intake``."""
        # Permission: if the existing match is owned by another user,
        # the service updates it. We mirror the previous behavior by
        # checking object perms only when an existing row is found.
        try:
            task, created = services.prodtask_intake(
                payload=dict(request.data),
                created_by=request.user.username,
            )
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        if not created:
            self.check_object_permissions(request, task)
        log_epicprod_action(
            'web', 'task_intake',
            subject_type='campaign_task', subject_key=task.composed_name,
            username=request.user.username,
            sublevel='normal', live_default=True, created=created)
        http_status = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(self.get_serializer(task).data, status=http_status)

    @action(detail=False, methods=['post'], url_path='record-submission')
    def record_submission(self, request):
        """
        Record outcome of a JEDI submission. Sets panda_task_id and status.
        Called by `pcs-task-cmd --submit` after Client.insertTaskParams()
        returns the JEDI task ID.

        Gates:
            - Task must be in status='ready'. No submit from draft;
              no re-submit from submitted/completed/failed.
            - Task must not already record a panda_task_id; refuses to
              overwrite (returns 409). Treats panda_task_id as one-shot.

        Query params:
            name — ProdTask.name (required)

        Body (JSON):
            jedi_task_id — int, required
            status       — str, optional (default 'submitted'); must be a
                           valid PRODTASK_STATUS_CHOICES value
        """
        name = request.query_params.get('name') or request.data.get('name')
        if not name:
            return Response({'detail': 'Missing ?name='},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            task = services.resolve_prodtask(name, self.get_queryset())
        except ProdTask.DoesNotExist:
            return Response({'detail': f"No task named '{name}'"},
                            status=status.HTTP_404_NOT_FOUND)
        self.check_object_permissions(request, task)

        try:
            services.prodtask_record_submission(
                task=task,
                jedi_task_id=request.data.get('jedi_task_id'),
                new_status=request.data.get('status', 'submitted'),
                panda_tasks_id=request.data.get('panda_tasks_id'),
                task_name=request.data.get('panda_task_name') or request.data.get('task_name'),
            )
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        return Response(self.get_serializer(task).data)

    @action(detail=False, methods=['post'], url_path='record-submission-failure')
    def record_submission_failure(self, request):
        """Record a failed pre-JEDI submission attempt on its PandaTasks row."""
        name = request.query_params.get('name') or request.data.get('name')
        if not name:
            return Response({'detail': 'Missing ?name='},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            task = services.resolve_prodtask(name, self.get_queryset())
        except ProdTask.DoesNotExist:
            return Response({'detail': f"No task named '{name}'"},
                            status=status.HTTP_404_NOT_FOUND)
        self.check_object_permissions(request, task)
        try:
            services.prodtask_record_submission_failure(
                task=task,
                panda_tasks_id=request.data.get('panda_tasks_id'),
                reason=request.data.get('reason', ''),
            )
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        return Response(self.get_serializer(task).data)


@api_view(['POST'])
@authentication_classes([TunnelAuthentication, SessionAuthentication,
                         TokenAuthentication])
@permission_classes([IsAuthenticated])
def prod_request_compose(request):
    """Create a production request from the request composer page.

    External-safe trigger: a /pcs/api/ POST returning JSON, so it works
    identically through the swf-remote proxy (EXTERNAL_ACCESS.md). The
    requester's working group is remembered in their per-user
    preferences for next time.
    """
    username = getattr(request.user, 'username', '') or ''
    fields = {
        key: request.data.get(key) or ''
        for key in ('pwg', 'dsc', 'description', 'process', 'beam',
                    'species', 'q2', 'generator', 'generator_version',
                    'sample', 'pc_anchor', 'simu_path', 'contact_name',
                    'contact_email', 'repository', 'intended_use')
    }
    try:
        result = services.prodrequest_compose(
            created_by=username,
            nevents=request.data.get('nevents'),
            **fields,
        )
    except ServiceError as e:
        return Response({'detail': e.detail}, status=e.status)
    from monitor_app.models import UserPreference
    if fields['pwg']:
        UserPreference.set_pref(username, 'composer_pwg', fields['pwg'])
    if fields['dsc']:
        UserPreference.set_pref(username, 'composer_dsc', fields['dsc'])
    UserPreference.set_pref(username, 'composer_contact_name',
                            fields['contact_name'])
    UserPreference.set_pref(username, 'composer_contact_email',
                            fields['contact_email'])
    return Response(result, status=status.HTTP_201_CREATED)
