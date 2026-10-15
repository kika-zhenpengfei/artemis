/**
 * Copyright 2026 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import { ChangeDetectionStrategy, Component, DestroyRef, OnInit, computed, inject, input } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DeviceSelectionService, SelectableDevice } from '../../services/device-selection.service';

@Component({
  selector: 'app-device-selector',
  standalone: true,
  imports: [FormsModule],
  templateUrl: './device-selector.component.html',
  styleUrl: './device-selector.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class DeviceSelectorComponent implements OnInit {
  public readonly deviceSelection = inject(DeviceSelectionService);
  public readonly disabled = input(false);
  private destroyRef = inject(DestroyRef);

  public readonly canRetrySelection = computed(() => {
    const device = this.deviceSelection.selectedDevice();
    return !this.disabled() && !this.deviceSelection.isLoading() && !!this.deviceSelection.adbEndpointId()
      && device?.state === 'device' && !device.is_busy && device.queued_count === 0;
  });

  public readonly hint = computed(() => {
    const error = this.deviceSelection.error();
    if (error) {
      return error;
    }
    if (this.deviceSelection.isLoading() && !this.deviceSelection.devices().length) {
      return 'Loading devices…';
    }
    const selectedSerial = this.deviceSelection.selectedSerial();
    const device = this.deviceSelection.selectedDevice();
    if (selectedSerial && !device) {
      return 'Selected device disconnected. Choose a device to continue.';
    }
    if (device?.state === 'unauthorized') {
      return 'Authorize this device to continue.';
    }
    if (device && device.state !== 'device') {
      return 'Selected device is offline.';
    }
    if (device?.is_busy) {
      return 'This device is busy.';
    }
    if (device && device.queued_count > 0) {
      return 'This device has queued tasks.';
    }
    if (!selectedSerial) {
      return this.deviceSelection.devices().some(item => item.state === 'device')
        ? 'Choose a device to start.' : 'No devices connected.';
    }
    return '';
  });

  public ngOnInit(): void {
    this.destroyRef.onDestroy(this.deviceSelection.watchDevices());
  }

  public selectDevice(serial: string): void {
    if (this.disabled()) {
      return;
    }
    this.deviceSelection.select(serial || null);
  }

  public retrySelection(): void {
    if (this.deviceSelection.selectionNeedsRetry() && this.canRetrySelection()) {
      this.deviceSelection.select(this.deviceSelection.selectedSerial());
    }
  }

  public deviceLabel(device: SelectableDevice): string {
    const model = device.model?.replace(/_/g, ' ');
    const name = model ? `${model} · ${device.serial}` : device.serial;
    return `${name} · ${this.deviceStatus(device)}`;
  }

  public deviceStatus(device: SelectableDevice): string {
    if (this.deviceSelection.error()) {
      return 'Unavailable';
    }
    if (device.state === 'unauthorized') {
      return 'Unauthorized';
    }
    if (device.state !== 'device') {
      return 'Offline';
    }
    const status = device.is_busy ? 'Busy' : 'Idle';
    return device.queued_count > 0 ? `${status} · Queued ${device.queued_count}` : status;
  }
}
