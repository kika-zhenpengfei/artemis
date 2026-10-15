import { provideHttpClient, withXhr } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { DeviceSelectionService, SelectableDevice } from '../../services/device-selection.service';
import { WaitingWorkflow, WorkflowDeviceService } from '../../services/workflow-device.service';
import { WorkflowDeviceQueueComponent } from './workflow-device-queue.component';

describe('WorkflowDeviceQueueComponent', () => {
  let fixture: ComponentFixture<WorkflowDeviceQueueComponent>;
  let component: WorkflowDeviceQueueComponent;
  let service: WorkflowDeviceService;
  let devices: DeviceSelectionService;
  let http: HttpTestingController;
  const endpoint = 'tcp:127.0.0.1:5037';
  const phone = (serial: string, changes: Partial<SelectableDevice> = {}): SelectableDevice => ({
    serial, state: 'device', model: 'Test phone', is_busy: false, queued_count: 0, ...changes
  });
  const first: WaitingWorkflow = {
    workflow_id: 'first', goal: 'Inspect Neon Love details', profile: 'pro', case_count: 2,
    status: 'awaiting_device', device_serial: null, adb_endpoint_id: null, error: null
  };
  const second: WaitingWorkflow = { ...first, workflow_id: 'second', goal: 'Inspect settings', profile: 'flash', case_count: 1 };
  const flushInitial = (endpointId = endpoint) => {
    http.expectOne('/api/devices').flush({ devices: [phone('a'), phone('b')], selected_serial: 'a', adb_endpoint_id: endpointId });
    http.expectOne('/api/test_workflows/waiting').flush({ workflows: [first, second] });
    fixture.detectChanges();
  };

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [WorkflowDeviceQueueComponent], providers: [provideHttpClient(withXhr()), provideHttpClientTesting()]
    }).compileComponents();
    fixture = TestBed.createComponent(WorkflowDeviceQueueComponent);
    component = fixture.componentInstance;
    service = TestBed.inject(WorkflowDeviceService);
    devices = TestBed.inject(DeviceSelectionService);
    http = TestBed.inject(HttpTestingController);
    fixture.detectChanges();
    flushInitial();
  });

  afterEach(() => {
    fixture.destroy();
    http.verify();
  });

  it('renders independent workflow choices without inheriting the global selected device', () => {
    expect(fixture.nativeElement.textContent).toContain('PRO · 2 cases');
    expect(fixture.nativeElement.textContent).toContain('FLASH · 1 case');
    const selects: NodeListOf<HTMLSelectElement> = fixture.nativeElement.querySelectorAll('select');
    expect(selects.length).toBe(2);
    expect(selects[0].value).toBe('');
    selects[0].value = 'b';
    selects[0].dispatchEvent(new Event('change'));
    fixture.detectChanges();
    expect(service.choice(service.workflows()[0]).serial).toBe('b');
    expect(service.choice(service.workflows()[1]).serial).toBe('');
    expect(devices.selectedSerial()).toBe('a');
    http.expectNone('/api/devices/selection');
  });

  it('preserves independent choices across tab or route destruction and revalidates their original endpoint', () => {
    service.chooseDevice('first', 'a');
    service.chooseDevice('second', 'b');
    fixture.destroy();
    fixture = TestBed.createComponent(WorkflowDeviceQueueComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    expect(service.canConfirm(service.workflows()[0])).toBeFalse();
    flushInitial('tcp:another-server:5037');
    expect(service.choice(service.workflows()[0])).toEqual({ serial: 'a', endpointId: endpoint, editing: true });
    expect(service.choice(service.workflows()[1]).serial).toBe('b');
    expect(service.canConfirm(service.workflows()[0])).toBeFalse();
    expect(fixture.nativeElement.textContent).toContain('Device connection changed');
  });

  it('disables unavailable options and retains a disconnected selection visibly', () => {
    service.chooseDevice('first', 'a');
    devices.refresh();
    http.expectOne('/api/devices').flush({
      devices: [phone('b', { is_busy: true }), phone('c', { queued_count: 1 }), phone('d', { state: 'unauthorized' })],
      selected_serial: 'a', adb_endpoint_id: endpoint
    });
    fixture.detectChanges();
    const select: HTMLSelectElement = fixture.nativeElement.querySelector('select');
    for (const serial of ['a', 'b', 'c', 'd']) {
      expect(select.querySelector<HTMLOptionElement>(`option[value="${serial}"]`)?.disabled).toBeTrue();
    }
    expect(fixture.nativeElement.textContent).toContain('a · Disconnected');
    expect(service.canConfirm(service.workflows()[0])).toBeFalse();
  });

  it('confirms a workflow without starting a task and explains the Codex handoff', () => {
    service.chooseDevice('first', 'b');
    fixture.detectChanges();
    const button: HTMLButtonElement = fixture.nativeElement.querySelector('.confirm-button');
    button.click();
    const post = http.expectOne('/api/test_workflows/first/confirm_device');
    post.flush({ workflow_id: first.workflow_id, status: 'confirmed', device_serial: 'b', adb_endpoint_id: endpoint, error: null });
    fixture.detectChanges();
    const card: HTMLElement = fixture.nativeElement.querySelector('article');
    expect(card.textContent).toContain('Inspect Neon Love details');
    expect(card.textContent).toContain('PRO · 2 cases');
    expect(card.textContent).toContain('Waiting for Codex to start.');
    expect(card.textContent).toContain('Device is checked again when execution starts.');
    expect(card.textContent).toContain('Confirmation does not reserve it.');
    expect(card.querySelector('select')).toBeNull();
    const change = Array.from(card.querySelectorAll('button')).find(item => item.textContent?.trim() === 'Change device');
    change?.click();
    fixture.detectChanges();
    expect(card.querySelector('select')).not.toBeNull();
    expect(service.choice(service.workflows()[0]).serial).toBe('');
    http.expectNone('/api/run');
    http.expectNone('/api/devices/selection');
  });

  it('locks starting workflows and hides the section after the waiting list becomes empty', () => {
    service.refresh();
    http.expectOne('/api/test_workflows/waiting').flush({ workflows: [{ ...first, status: 'starting', device_serial: 'a', adb_endpoint_id: endpoint }] });
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('Codex is starting execution.');
    expect(fixture.nativeElement.querySelector('select')).toBeNull();
    expect(fixture.nativeElement.querySelector('.confirm-button')).toBeNull();
    service.refresh();
    http.expectOne('/api/test_workflows/waiting').flush({ workflows: [] });
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('section')).toBeNull();
  });
});
