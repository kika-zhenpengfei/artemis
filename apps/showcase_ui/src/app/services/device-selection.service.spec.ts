import { provideHttpClient, withXhr } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed, fakeAsync, tick } from '@angular/core/testing';

import { DeviceSelectionService, SelectableDevice } from './device-selection.service';

describe('DeviceSelectionService', () => {
  let service: DeviceSelectionService;
  let http: HttpTestingController;
  const phone = (serial: string, overrides: Partial<SelectableDevice> = {}): SelectableDevice => ({
    serial, state: 'device', model: 'Test phone', is_busy: false, queued_count: 0, ...overrides
  });

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(withXhr()), provideHttpClientTesting()]
    });
    service = TestBed.inject(DeviceSelectionService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('requires an explicit selection even when only one idle device is online', () => {
    service.refresh();
    http.expectOne('/api/devices').flush({ adb_endpoint_id: 'tcp:127.0.0.1:5037', devices: [phone('a')], selected_serial: null });
    expect(service.selectedSerial()).toBeNull();
    expect(service.canStart()).toBeFalse();
  });

  it('waits for a fresh device snapshot after saving the chosen serial', () => {
    const stop = service.watchDevices();
    http.expectOne('/api/devices').flush({ adb_endpoint_id: 'tcp:127.0.0.1:5037', devices: [phone('a')], selected_serial: null });
    service.select('a');
    const request = http.expectOne('/api/devices/selection');
    expect(request.request.body).toEqual({ serial: 'a', expected_adb_endpoint_id: 'tcp:127.0.0.1:5037' });
    expect(service.canStart()).toBeFalse();
    request.flush({ selected_serial: 'a' });
    expect(service.canStart()).toBeFalse();
    http.expectOne('/api/devices').flush({ adb_endpoint_id: 'tcp:127.0.0.1:5037', devices: [phone('a')], selected_serial: 'a' });
    expect(service.canStart()).toBeTrue();
    stop();
  });

  it('blocks busy, queued and disconnected selections without switching to another idle phone', () => {
    for (const devices of [
      [phone('a', { is_busy: true }), phone('b')],
      [phone('a', { queued_count: 1 }), phone('b')],
      [phone('a', { state: 'offline' }), phone('b')],
      [phone('b')]
    ]) {
      service.refresh();
      http.expectOne('/api/devices').flush({ adb_endpoint_id: 'tcp:127.0.0.1:5037', devices, selected_serial: 'a' });
      expect(service.selectedSerial()).toBe('a');
      expect(service.canStart()).toBeFalse();
    }
  });

  it('invalidates the previous idle snapshot when device enumeration fails', () => {
    service.refresh();
    http.expectOne('/api/devices').flush({ adb_endpoint_id: 'tcp:127.0.0.1:5037', devices: [phone('a')], selected_serial: 'a' });
    expect(service.canStart()).toBeTrue();
    service.refresh();
    http.expectOne('/api/devices').flush({ detail: 'ADB unavailable' }, { status: 503, statusText: 'Unavailable' });
    expect(service.canStart()).toBeFalse();
    expect(service.error()).toBe('ADB unavailable');
  });

  it('uses the selection belonging to the current ADB server even when serials match', () => {
    service.refresh();
    http.expectOne('/api/devices').flush({
      adb_endpoint_id: 'tcp:127.0.0.1:5037', devices: [phone('emulator-5554')], selected_serial: 'emulator-5554'
    });
    expect(service.canStart()).toBeTrue();
    service.refresh();
    http.expectOne('/api/devices').flush({
      adb_endpoint_id: 'tcp:192.0.2.10:5037', devices: [phone('emulator-5554')], selected_serial: null
    });
    expect(service.selectedSerial()).toBeNull();
    expect(service.adbEndpointId()).toBe('tcp:192.0.2.10:5037');
    expect(service.canStart()).toBeFalse();
    service.select('emulator-5554');
    const request = http.expectOne('/api/devices/selection');
    expect(request.request.body).toEqual({
      serial: 'emulator-5554', expected_adb_endpoint_id: 'tcp:192.0.2.10:5037'
    });
    request.flush({ selected_serial: 'emulator-5554' });
  });

  it('retains a failed choice instead of silently reverting and permits an explicit retry', () => {
    const stop = service.watchDevices();
    http.expectOne('/api/devices').flush({ adb_endpoint_id: 'tcp:127.0.0.1:5037', devices: [phone('a'), phone('b')], selected_serial: 'a' });
    service.select('b');
    http.expectOne('/api/devices/selection').flush({ detail: 'Device became busy' }, { status: 409, statusText: 'Conflict' });
    service.refresh();
    http.expectOne('/api/devices').flush({ adb_endpoint_id: 'tcp:127.0.0.1:5037', devices: [phone('a'), phone('b')], selected_serial: 'a' });
    expect(service.selectedSerial()).toBe('b');
    expect(service.selectionNeedsRetry()).toBeTrue();
    expect(service.error()).toBe('Device became busy');
    expect(service.canStart()).toBeFalse();
    service.select('b');
    http.expectOne('/api/devices/selection').flush({ selected_serial: 'b' });
    http.expectOne('/api/devices').flush({ adb_endpoint_id: 'tcp:127.0.0.1:5037', devices: [phone('a'), phone('b')], selected_serial: 'b' });
    expect(service.selectionNeedsRetry()).toBeFalse();
    expect(service.canStart()).toBeTrue();
    stop();
  });

  it('cancels an old refresh before changing the selection', () => {
    service.refresh();
    http.expectOne('/api/devices').flush({
      adb_endpoint_id: 'tcp:127.0.0.1:5037', devices: [phone('a'), phone('b')], selected_serial: 'a'
    });
    service.refresh();
    const stale = http.expectOne('/api/devices');
    service.select('b');
    expect(stale.cancelled).toBeTrue();
    http.expectOne('/api/devices/selection').flush({ selected_serial: 'b' });
    expect(service.selectedSerial()).toBe('b');
    expect(service.canStart()).toBeFalse();
  });

  it('does not submit a device from a stale list when its ADB server is unknown', () => {
    service.select('a');
    http.expectNone('/api/devices/selection');
    expect(service.canStart()).toBeFalse();
    expect(service.error()).toContain('Refresh');
  });

  it('shares polling and stops after the last visible selector is destroyed', fakeAsync(() => {
    const stopFirst = service.watchDevices();
    const stopSecond = service.watchDevices();
    http.expectOne('/api/devices').flush({ adb_endpoint_id: 'tcp:127.0.0.1:5037', devices: [phone('a')], selected_serial: 'a' });
    stopFirst();
    tick(5000);
    http.expectOne('/api/devices').flush({ adb_endpoint_id: 'tcp:127.0.0.1:5037', devices: [phone('a')], selected_serial: 'a' });
    stopSecond();
    tick(10000);
    http.expectNone('/api/devices');
    expect(service.canStart()).toBeFalse();
  }));
});
