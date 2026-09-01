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
    Dataset, EvgenMark, ProdConfig, ProdTask, PandaTasks, Questionnaire,
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

    @action(detail=False, methods=['post'], url_path='expected-events')
    def expected_events(self, request):
        """Single or bulk expected-events target set with required comment.

        Body: ``entries`` (list of ``{name, expected_events, source}``;
        ``expected_events: null`` clears), ``comment`` (required). Thin
        wrapper over ``services.dataset_expected_events_set``; one
        action-stream event per call. CAMPAIGN_DELIVERY.md extension 1.
        """
        try:
            result = services.dataset_expected_events_set(
                request.data.get('entries') or [],
                request.data.get('comment'),
                changed_by=request.user.username,
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

    def list(self, request, *args, **kwargs):
        # The PWG priority of every task from two queries (the marks and
        # the evgen datasets' paths by tags), not a rescan per task: the
        # unpaginated list is thousands of tasks.
        from .models import annotate_pwg_priority
        tasks = annotate_pwg_priority(
            self.filter_queryset(self.get_queryset()))
        return Response(self.get_serializer(tasks, many=True).data)

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
                residual = request.query_params.get('residual') in ('1', 'true')
                return JsonResponse(
                    build_evgen_task_params(task, panda_tasks=panda_tasks,
                                            residual=residual),
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

    @action(detail=True, methods=['get'], url_path='residual-preview')
    def residual_preview(self, request, name=None):
        """The residual coverage a Rerun Residual would submit: rows_total,
        rows_residual, checked_dids — or the refusal reason. Read-only;
        performs the JLab listing on demand (button-gated, never in a
        page render)."""
        task = self.get_object()
        from .commands import build_evgen_task_params
        try:
            spec = build_evgen_task_params(task, residual=True)
        except ValueError as e:
            return Response({'refusal': str(e)})
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        return Response(spec['residual'])

    @action(detail=True, methods=['post'], url_path='rerun-residual')
    def rerun_residual(self, request, name=None):
        """Queue a residual .tryN submission over the undelivered
        remainder (docs/JEDI_INTEGRATION.md § Residual rerun)."""
        task = self.get_object()
        try:
            services.prodtask_rerun_residual_request(task=task)
        except ServiceError as e:
            return Response({'detail': e.detail}, status=e.status)
        return Response(self.get_serializer(task).data)

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
                residual=request.data.get('residual'),
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
                    'contact_email', 'repository', 'intended_use',
                    'background_mode', 'background_tag', 'background_other')
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


@api_view(['POST'])
@authentication_classes([TunnelAuthentication, SessionAuthentication,
                         TokenAuthentication])
@permission_classes([IsAuthenticated])
def physics_configs_requestors(request):
    """Single or bulk requestors set on physics configurations with a
    required comment. Body: ``entries`` (list of ``{config,
    requestors}``), ``comment``. Thin wrapper over
    ``services.physics_config_requestors_set``; one action-stream event
    per call."""
    try:
        result = services.physics_config_requestors_set(
            request.data.get('entries') or [],
            request.data.get('comment'),
            changed_by=request.user.username,
        )
    except ServiceError as e:
        return Response({'detail': e.detail}, status=e.status)
    return Response(result, status=status.HTTP_200_OK)


@api_view(['GET'])
@authentication_classes([TunnelAuthentication, SessionAuthentication,
                         TokenAuthentication])
@permission_classes([IsAuthenticatedOrReadOnly])
def campaigns_status(request):
    """Campaign status rollup — the assessment evidence document
    (docs/EPICPROD_ASSESSMENTS_V1.md). Read-only, no DB writes beyond
    SysConfig autoseeding of unset threshold keys.

    Query params:
        campaign     — campaign name; default: first producing campaign,
                       else current.
        window_days  — activity window for deltas/flips/actions (default 1).
        history_at   — return the recorded production snapshot closest to
                       this ISO-8601 timestamp instead of computing live state.
        targets_only — '1' returns only {targets, assessment_enabled}, the
                       cheap form the scheduled trigger polls.
    """
    from swf_epicprod.analytics.rollup import (
        campaign_status as _campaign_status,
        campaign_status_snapshot,
        resolve_target_campaigns,
    )

    if request.query_params.get('targets_only') in ('1', 'true'):
        from monitor_app.models import SysConfig
        return Response({
            'targets': resolve_target_campaigns(),
            'assessment_enabled': bool(
                SysConfig.get_setting('assessment_enabled', True)),
        })
    try:
        if request.query_params.get('history_at'):
            return Response(campaign_status_snapshot(
                request.query_params.get('campaign') or '',
                request.query_params['history_at']))
        result = _campaign_status(
            request.query_params.get('campaign') or None,
            window_days=request.query_params.get('window_days') or 1)
    except ServiceError as e:
        return Response({'detail': str(e)}, status=getattr(e, 'status', 400))
    return Response(result)


# ---------------------------------------------------------------------------
# Validation interface v1 (EPICPROD_VALIDATION.md § REST interface)
# ---------------------------------------------------------------------------

from drf_spectacular.utils import (OpenApiExample, extend_schema,
                                   inline_serializer)
from drf_spectacular.types import OpenApiTypes
from rest_framework import serializers as drf_serializers

_COMPLETION_EXAMPLE = OpenApiExample(
    'sample completion',
    value={
        'sample': 'group.EIC.26.07.1.epic_craterlake.p2339.e1.s1.r1',
        'campaign': '26.07',
        'revision': 1,
        'events_delivered': 5000000,
        'event_target': 5000000,
        'event_target_source': 'included',
        'complete': 'yes',
        'completion_basis': 'events',
        'rucio': ['epic:/RECO/26.07.1/epic_craterlake/DIS/'
                  'pythia8.316-1.0/NC/noRad/ep/9x275/q2_1to10'],
        'catalog_url': '/swf-monitor/pcs/api/v1/campaigns/26.07/catalog/',
    },
    response_only=True,
)

_VALIDATION_RESULT_REQUEST = inline_serializer(
    name='ValidationResultRequest',
    fields={
        'sample': drf_serializers.CharField(
            help_text='PCS composed name of the validated sample'),
        'revision': drf_serializers.IntegerField(
            required=False, default=1, min_value=1,
            help_text='Sample delivery revision the result judges'),
        'status': drf_serializers.ChoiceField(
            choices=['pending', 'running', 'validated', 'failed']),
        'benchmarks': drf_serializers.ListField(
            child=drf_serializers.DictField(), required=False,
            help_text='Per-benchmark outcomes '
                      '(name, status, events_used, report_url)'),
        'invalidated': drf_serializers.ListField(
            child=drf_serializers.CharField(), required=False,
            help_text='Rucio references no longer counting toward the '
                      'sample; empty on failure means the whole sample'),
        'completed_at': drf_serializers.DateTimeField(required=False),
        'details_url': drf_serializers.URLField(required=False),
    },
)


@extend_schema(tags=['validation'], responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@authentication_classes([TunnelAuthentication, SessionAuthentication,
                         TokenAuthentication])
@permission_classes([IsAuthenticatedOrReadOnly])
def validation_v1_index(request):
    """Index of the validation interface v1: its endpoints and where the
    interactive documentation lives. Open read-only."""
    from django.urls import reverse
    base = reverse('pcs:validation_v1_index')
    return Response({
        'interface': 'epicprod validation v1',
        'documentation': {
            'swagger': reverse('swagger-ui'),
            'redoc': reverse('redoc'),
            'schema': reverse('schema'),
            'design': 'https://github.com/BNLNPPS/swf-epicprod/blob/main/'
                      'docs/EPICPROD_VALIDATION.md',
        },
        'endpoints': {
            'sample_completion': base + 'samples/{sample}/completion/',
            'campaign_completion': base + 'campaigns/{campaign}/completion/',
            'campaign_catalog': base + 'campaigns/{campaign}/catalog/',
            'validation_results': base + 'validation-results/',
        },
    })


@extend_schema(tags=['validation'],
               responses={200: OpenApiTypes.OBJECT},
               examples=[_COMPLETION_EXAMPLE])
@api_view(['GET'])
@authentication_classes([TunnelAuthentication, SessionAuthentication,
                         TokenAuthentication])
@permission_classes([IsAuthenticatedOrReadOnly])
def validation_sample_completion(request, sample):
    """Completion state for one sample — the availability-signal body
    served for pulling (EPICPROD_VALIDATION.md § Completion pull). Open
    read-only. ``sample`` is the PCS composed name."""
    try:
        task = services.resolve_prodtask(sample)
    except services.AmbiguousIdentity as exc:
        return Response(
            {'detail': f"Ambiguous sample '{sample}': "
                       f"{len(exc.matches)} tasks match",
             'candidates': [t.name for t in exc.matches]},
            status=status.HTTP_300_MULTIPLE_CHOICES)
    except ProdTask.DoesNotExist:
        return Response({'detail': f"No sample '{sample}'"},
                        status=status.HTTP_404_NOT_FOUND)
    return Response(services.sample_completion_payload(task))


@extend_schema(tags=['validation'], responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@authentication_classes([TunnelAuthentication, SessionAuthentication,
                         TokenAuthentication])
@permission_classes([IsAuthenticatedOrReadOnly])
def validation_campaign_completion(request, campaign):
    """Per-sample completion across a campaign. Open read-only.
    ``campaign`` is the two-part family; a detector version is accepted
    and truncated to its family."""
    try:
        payload = services.campaign_completion_payload(campaign)
    except ServiceError as e:
        return Response({'detail': e.detail}, status=e.status)
    return Response(payload)


@extend_schema(tags=['validation'], responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@authentication_classes([TunnelAuthentication, SessionAuthentication,
                         TokenAuthentication])
@permission_classes([IsAuthenticatedOrReadOnly])
def validation_campaign_catalog(request, campaign):
    """The campaign catalog document — the complete machine-readable
    description of a campaign and its samples. Open read-only."""
    try:
        payload = services.campaign_catalog_payload(campaign)
    except ServiceError as e:
        return Response({'detail': e.detail}, status=e.status)
    return Response(payload)


@extend_schema(tags=['validation'],
               request=_VALIDATION_RESULT_REQUEST,
               responses={201: OpenApiTypes.OBJECT})
@api_view(['POST'])
@authentication_classes([TunnelAuthentication, SessionAuthentication,
                         TokenAuthentication])
@permission_classes([IsAuthenticated])
def validation_results_receive(request):
    """The validation system transmits each finished result JSON here
    (EPICPROD_VALIDATION.md § Result notification). Token-authenticated
    write; receipt is stored append-only, mirrored onto the sample's
    Dataset rows, and logged to the action stream."""
    try:
        result = services.validation_result_receive(
            dict(request.data),
            received_from=getattr(request.user, 'username', '') or '')
    except ServiceError as e:
        return Response({'detail': e.detail}, status=e.status)
    return Response(result, status=status.HTTP_201_CREATED)


@api_view(['POST'])
@authentication_classes([TunnelAuthentication, SessionAuthentication,
                         TokenAuthentication])
@permission_classes([IsAuthenticated])
def evgen_register(request):
    """Queue the registration of EVGEN input directories in JLab Rucio —
    the EVGEN inputs page's "Register in Rucio" action on the coverage
    worklist and its free path box. Body: {"paths": ["/EVGEN/...", ...]}
    (a path may also be given as the /volatile door path or the root://
    URL). Each path is validated and queued independently
    (services.evgen_register_request): the reply lists the queued
    normalized paths and the refused ones with the reason. The web tier
    holds no credential; the prod-ops agent registers and then
    re-assimilates the inventory, pushing evgen_register_ready and
    evgen_rucio_ready over the SSE relay. Signed-in users only; works
    identically on the internal face and through the swf-remote proxy
    (JSON in, JSON out, no redirect). See docs/EPICPROD_EVGEN_INPUTS.md
    § Registration."""
    data = request.data if isinstance(request.data, dict) else {}
    paths = data.get('paths')
    if (not isinstance(paths, list) or not paths
            or not all(isinstance(p, str) and p.strip() for p in paths)):
        return Response({'detail': 'body must be {"paths": ["/EVGEN/...", ...]}'},
                        status=status.HTTP_400_BAD_REQUEST)
    username = getattr(request.user, 'username', '') or ''
    # A Rucio write under the production account: the tunnel fallback
    # identity (an anonymous external or bare-localhost request) may
    # read, never register.
    if not username or username == 'swf-remote-proxy':
        return Response({'detail': 'sign in to register EVGEN data'},
                        status=status.HTTP_403_FORBIDDEN)
    convention = services.evgen_convention_paths_cached()
    queued, refused = [], []
    for raw in paths:
        try:
            queued.append(services.evgen_register_request(
                path=raw, created_by=username, convention=convention))
        except ServiceError as e:
            refused.append({'path': raw.strip(), 'reason': e.detail})
    log_epicprod_action(
        'web', 'evgen_register_request',
        subject_type='evgen_path',
        subject_key=queued[0] if len(queued) == 1 else f'{len(queued)} paths',
        username=username, sublevel='high', live_default=True,
        outcome='ok' if queued else 'error',
        reason=('' if queued else '; '.join(
            f"{r['path']}: {r['reason']}" for r in refused)[:300]),
        queued=len(queued), refused=len(refused), paths=queued[:20])
    return Response({'ok': bool(queued), 'queued': queued, 'refused': refused},
                    status=(status.HTTP_202_ACCEPTED if queued
                            else status.HTTP_400_BAD_REQUEST))


@api_view(['POST'])
@authentication_classes([TunnelAuthentication, SessionAuthentication,
                         TokenAuthentication])
@permission_classes([IsAuthenticated])
def evgen_mark(request):
    """Set a PWG mark on EVGEN paths — the EVGEN inputs page's tick-box
    actions and the per-row priority buttons, for PWG triage of the
    inventory and the registration worklist. Body: {"paths":
    ["/EVGEN/..."]} plus one of "obsolete": true|false (with "comment",
    required when marking obsolete) or "priority": 0|1|2|3 (0 clears).
    The two marks keep separate attribution. One call per save, one
    action-stream event; works identically on the internal face and
    through the swf-remote proxy (EXTERNAL_ACCESS.md write contract:
    JSON in, JSON out, no redirect)."""
    from django.utils import timezone as dj_timezone

    data = request.data if isinstance(request.data, dict) else {}
    paths = data.get('paths')
    obsolete = data.get('obsolete')
    priority = data.get('priority')
    comment = str(data.get('comment') or '').strip()
    usage = ('body must be {"paths": ["/EVGEN/..."]} with either '
             '"obsolete": true|false (and "comment") or "priority": 0|1|2|3')
    if (not isinstance(paths, list) or not paths
            or not all(isinstance(p, str) and p.startswith('/EVGEN/')
                       for p in paths)):
        return Response({'detail': usage}, status=status.HTTP_400_BAD_REQUEST)
    if (obsolete is None) == (priority is None):
        return Response({'detail': usage}, status=status.HTTP_400_BAD_REQUEST)
    if obsolete is not None and not isinstance(obsolete, bool):
        return Response({'detail': usage}, status=status.HTTP_400_BAD_REQUEST)
    if priority is not None and (
            isinstance(priority, bool) or not isinstance(priority, int)
            or priority not in (0,) + EvgenMark.PRIORITY_LEVELS):
        return Response({'detail': usage}, status=status.HTTP_400_BAD_REQUEST)
    if obsolete and not comment:
        return Response(
            {'detail': 'a comment is required to mark data obsolete'},
            status=status.HTTP_400_BAD_REQUEST)
    username = getattr(request.user, 'username', '') or ''
    # The mark's whole point is attribution: the tunnel fallback
    # identity (an anonymous external or bare-localhost request) may
    # read, never mark.
    if not username or username == 'swf-remote-proxy':
        return Response({'detail': 'sign in to mark EVGEN data'},
                        status=status.HTTP_403_FORBIDDEN)
    now = dj_timezone.now()
    if obsolete is not None:
        for p in paths:
            EvgenMark.objects.update_or_create(
                path=p, defaults={'obsolete': obsolete, 'set_by': username,
                                  'set_at': now, 'comment': comment})
        log_epicprod_action(
            'web', 'evgen_mark_obsolete',
            subject_type='evgen_path',
            subject_key=paths[0] if len(paths) == 1 else f'{len(paths)} paths',
            username=username,
            sublevel='high', live_default=True,
            obsolete=obsolete, count=len(paths), comment=comment,
            paths=paths[:20])
        return Response({'ok': True, 'updated': len(paths),
                         'obsolete': obsolete})
    for p in paths:
        EvgenMark.objects.update_or_create(
            path=p, defaults={'priority': priority,
                              'priority_set_by': username,
                              'priority_set_at': now})
    log_epicprod_action(
        'web', 'evgen_mark_priority',
        subject_type='evgen_path',
        subject_key=paths[0] if len(paths) == 1 else f'{len(paths)} paths',
        username=username,
        sublevel='low', live_default=False,
        priority=priority, count=len(paths), paths=paths[:20])
    return Response({'ok': True, 'updated': len(paths),
                     'priority': priority})
