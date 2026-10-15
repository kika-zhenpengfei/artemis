import { provideHttpClient, withXhr } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed, fakeAsync, tick } from '@angular/core/testing';
import { DeviceSelectionService, SelectableDevice } from './device-selection.service';
import { WaitingWorkflow, WorkflowDeviceService } from './workflow-device.service';

describe('WorkflowDeviceService', () => {
  let service: WorkflowDeviceService;
  let devices: DeviceSelectionService;
  let http: HttpTestingController;
  const endpoint = 'tcp:127.0.0.1:5037';
  const phone = (serial: string, changes: Partial<SelectableDevice> = {}): SelectableDevice => ({
    serial, state: 'device', model: 'Test phone', is_busy: false, queued_count: 0, ...changes
  });
  const workflow = (id: string, changes: Partial<WaitingWorkflow> = {}): WaitingWorkflow => ({
    workflow_id: id, goal: `Goal ${id}`, profile: 'pro', case_count: 2,
    status: 'awaiting_device', device_serial: null, adb_endpoint_id: null, error: null, ...changes
  });
  const loadDevices = (list = [phone('a'), phone('b')], endpointId = endpoint) => {
    devices.refresh();
    http.expectOne('/api/devices').flush({ devices: list, selected_serial: 'a', adb_endpoint_id: endpointId });
  };
  const loadWorkflows = (list = [workflow('first'), workflow('second')]) => {
    service.refresh();
    http.expectOne('/api/test_workflows/waiting').flush({ workflows: list });
  };

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(withXhr()), provideHttpClientTesting()] });
    service = TestBed.inject(WorkflowDeviceService);
    devices = TestBed.inject(DeviceSelectionService);
    http = TestBed.inject(HttpTestingController);
    loadDevices();
    loadWorkflows();
  });

  afterEach(() => http.verify());

  it('confirms independent workflows concurrently without changing the shared default or launching tasks', () => {
    expect(service.choice(service.workflows()[0]).serial).toBe('');
    const selectDefault = spyOn(devices, 'select');
    service.chooseDevice('first', 'a');
    service.chooseDevice('second', 'b');
    service.confirm('first');
    service.confirm('first');
    service.confirm('second');
    const first = http.expectOne('/api/test_workflows/first/confirm_device');
    const second = http.expectOne('/api/test_workflows/second/confirm_device');
    expect(first.request.body).toEqual({ device_serial: 'a', expected_adb_endpoint_id: endpoint });
    expect(second.request.body).toEqual({ device_serial: 'b', expected_adb_endpoint_id: endpoint });
    expect(service.isConfirming('first')).toBeTrue();
    expect(service.isConfirming('second')).toBeTrue();
    second.flush(workflow('second', { status: 'confirmed', device_serial: 'b', adb_endpoint_id: endpoint }));
    expect(service.isConfirming('first')).toBeTrue();
    expect(service.isConfirming('second')).toBeFalse();
    first.flush(workflow('first', { status: 'confirmed', device_serial: 'a', adb_endpoint_id: endpoint }));
    expect(service.workflows().every(item => item.status === 'confirmed')).toBeTrue();
    expect(selectDefault).not.toHaveBeenCalled();
    expect(devices.selectedSerial()).toBe('a');
    http.expectNone('/api/devices/selection');
    http.expectNone('/api/run');
  });

  it('checks busy, queued, unauthorized and disconnected devices before confirming', () => {
    service.chooseDevice('first', 'a');
    for (const list of [
      [phone('a', { is_busy: true }), phone('b')],
      [phone('a', { queued_count: 1 }), phone('b')],
      [phone('a', { state: 'unauthorized' }), phone('b')],
      [phone('b')]
    ]) {
      loadDevices(list);
      expect(service.canConfirm(service.workflows()[0])).toBeFalse();
      expect(service.choice(service.workflows()[0]).serial).toBe('a');
      service.confirm('first');
      http.expectNone('/api/test_workflows/first/confirm_device');
    }
  });

  it('binds each choice to the endpoint observed when it was selected', () => {
    service.chooseDevice('first', 'a');
    loadDevices([phone('a'), phone('b')], 'tcp:another-server:5037');
    expect(service.canConfirm(service.workflows()[0])).toBeFalse();
    expect(service.selectionHint(service.workflows()[0])).toContain('connection changed');
    expect(service.choice(service.workflows()[0]).endpointId).toBe(endpoint);
    service.changeDevice('first');
    service.chooseDevice('first', 'a');
    service.confirm('first');
    const request = http.expectOne('/api/test_workflows/first/confirm_device');
    expect(request.request.body.expected_adb_endpoint_id).toBe('tcp:another-server:5037');
    request.flush(workflow('first', { status: 'confirmed', device_serial: 'a', adb_endpoint_id: 'tcp:another-server:5037' }));
  });

  it('retains a failed choice and recovers a lost confirmation response from polling', () => {
    service.chooseDevice('first', 'b');
    service.refresh();
    const stale = http.expectOne('/api/test_workflows/waiting');
    service.confirm('first');
    expect(stale.cancelled).toBeTrue();
    http.expectOne('/api/test_workflows/first/confirm_device').error(new ProgressEvent('error'));
    expect(service.errorFor(service.workflows()[0])).toContain('Could not confirm');
    expect(service.choice(service.workflows()[0]).serial).toBe('b');
    loadWorkflows([workflow('first', { status: 'confirmed', device_serial: 'b', adb_endpoint_id: endpoint })]);
    expect(service.errorFor(service.workflows()[0])).toBeNull();
    expect(service.isEditing(service.workflows()[0])).toBeFalse();
    expect(service.canConfirm(service.workflows()[0])).toBeFalse();
  });

  it('allows explicit changes to a confirmed choice until Codex starts', () => {
    loadWorkflows([workflow('first', { status: 'confirmed', device_serial: 'a', adb_endpoint_id: endpoint })]);
    service.chooseDevice('first', 'b');
    expect(service.choice(service.workflows()[0]).serial).toBe('a');
    service.changeDevice('first');
    service.chooseDevice('first', 'b');
    loadWorkflows([workflow('first', { status: 'confirmed', device_serial: 'a', adb_endpoint_id: endpoint })]);
    expect(service.choice(service.workflows()[0]).serial).toBe('b');
    expect(service.canConfirm(service.workflows()[0])).toBeTrue();
    loadWorkflows([workflow('first', { status: 'starting', device_serial: 'a', adb_endpoint_id: endpoint })]);
    service.changeDevice('first');
    service.chooseDevice('first', 'b');
    service.confirm('first');
    expect(service.isEditing(service.workflows()[0])).toBeFalse();
    expect(service.choice(service.workflows()[0]).serial).toBe('a');
    http.expectNone('/api/test_workflows/first/confirm_device');
  });

  it('preserves server errors when device admission fails and permits reselection', () => {
    service.chooseDevice('first', 'a');
    service.confirm('first');
    http.expectOne('/api/test_workflows/first/confirm_device').flush(
      { detail: 'Device is now busy' }, { status: 409, statusText: 'Conflict' }
    );
    expect(service.errorFor(service.workflows()[0])).toBe('Device is now busy');
    service.chooseDevice('first', 'b');
    expect(service.errorFor(service.workflows()[0])).toBeNull();
    expect(service.canConfirm(service.workflows()[0])).toBeTrue();
  });

  it('fails closed when either workflow or device refresh fails', () => {
    service.chooseDevice('first', 'a');
    service.refresh();
    http.expectOne('/api/test_workflows/waiting').flush({ detail: 'Workflow store unavailable' }, { status: 503, statusText: 'Unavailable' });
    expect(service.canConfirm(service.workflows()[0])).toBeFalse();
    loadWorkflows();
    devices.refresh();
    http.expectOne('/api/devices').flush({ detail: 'ADB unavailable' }, { status: 503, statusText: 'Unavailable' });
    expect(service.canConfirm(service.workflows()[0])).toBeFalse();
    service.confirm('first');
    http.expectNone('/api/test_workflows/first/confirm_device');
  });

  it('shares one three-second poll and stops after the final view is destroyed', fakeAsync(() => {
    const firstStop = service.watchWorkflows();
    const secondStop = service.watchWorkflows();
    const first = http.expectOne('/api/test_workflows/waiting');
    tick(3000);
    http.expectNone('/api/test_workflows/waiting');
    first.flush({ workflows: [workflow('first')] });
    firstStop();
    tick(3000);
    const second = http.expectOne('/api/test_workflows/waiting');
    expect(service.isLoading()).toBeTrue();
    secondStop();
    expect(second.cancelled).toBeTrue();
    tick(6000);
    http.expectNone('/api/test_workflows/waiting');
    expect(service.isLoading()).toBeFalse();
  }));

  it('removes workflows and their choices after execution leaves the waiting list', () => {
    service.chooseDevice('first', 'a');
    loadWorkflows([]);
    expect(service.workflows()).toEqual([]);
    loadWorkflows([workflow('first')]);
    expect(service.choice(service.workflows()[0]).serial).toBe('');
  });
});
