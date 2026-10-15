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

import { DestroyRef, Injectable, computed, inject, signal } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Subscription, finalize, timeout } from 'rxjs';

export interface SelectableDevice {
  serial: string;
  state: string;
  model: string | null;
  is_busy: boolean;
  queued_count: number;
}

export interface DeviceSelectionResponse {
  selected_serial: string | null;
}

export interface DeviceListResponse extends DeviceSelectionResponse {
  adb_endpoint_id: string;
  devices: SelectableDevice[];
}

@Injectable({ providedIn: 'root' })
export class DeviceSelectionService {
  private http = inject(HttpClient);
  private deviceState = signal<SelectableDevice[]>([]);
  private selectedSerialState = signal<string | null>(null);
  private adbEndpointState = signal<string | null>(null);
  private refreshPending = signal(false);
  private selectionPending = signal(false);
  private snapshotValid = signal(false);
  private errorState = signal<string | null>(null);
  private selectionFailed = signal(false);
  private refreshRequest: Subscription | null = null;
  private selectionRequest: Subscription | null = null;
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private watcherCount = 0;

  public readonly devices = this.deviceState.asReadonly();
  public readonly selectedSerial = this.selectedSerialState.asReadonly();
  public readonly adbEndpointId = this.adbEndpointState.asReadonly();
  public readonly selectedDevice = computed(() =>
    this.devices().find(device => device.serial === this.selectedSerial()) ?? null
  );
  public readonly isLoading = computed(() => this.refreshPending() || this.selectionPending());
  public readonly error = this.errorState.asReadonly();
  public readonly selectionNeedsRetry = this.selectionFailed.asReadonly();
  public readonly canStart = computed(() => {
    const device = this.selectedDevice();
    return this.snapshotValid() && !!this.adbEndpointId() && !this.isLoading() && !this.error()
      && device?.state === 'device' && !device.is_busy && device.queued_count === 0;
  });

  constructor() {
    inject(DestroyRef).onDestroy(() => {
      this.stopPolling();
      this.selectionRequest?.unsubscribe();
    });
  }

  public watchDevices(): () => void {
    this.watcherCount++;
    if (this.watcherCount === 1) {
      this.refresh();
      this.pollTimer = setInterval(() => this.refresh(), 5000);
    }
    let watching = true;
    return () => {
      if (!watching) {
        return;
      }
      watching = false;
      this.watcherCount--;
      if (this.watcherCount === 0) {
        this.stopPolling();
      }
    };
  }

  public refresh(): void {
    if (this.refreshPending() || this.selectionPending()) {
      return;
    }
    this.refreshPending.set(true);
    this.refreshRequest = this.http.get<DeviceListResponse>('/api/devices').pipe(
      timeout(10000),
      finalize(() => {
        this.refreshPending.set(false);
        this.refreshRequest = null;
      })
    ).subscribe({
      next: (response) => {
        this.adbEndpointState.set(response.adb_endpoint_id);
        this.deviceState.set(response.devices);
        // A failed selection must not silently revert to the previous server selection.
        if (!this.selectionFailed() || response.selected_serial === this.selectedSerial()) {
          this.selectionFailed.set(false);
          this.selectedSerialState.set(response.selected_serial);
          this.snapshotValid.set(true);
          this.errorState.set(null);
        }
      },
      error: (error: HttpErrorResponse) => {
        this.snapshotValid.set(false);
        this.adbEndpointState.set(null);
        if (!this.selectionFailed()) {
          this.errorState.set(this.errorMessage(error, 'Could not refresh devices. Try again.'));
        }
      }
    });
  }

  public select(serial: string | null): void {
    if (this.selectionPending()) {
      return;
    }
    if (serial && !this.adbEndpointId()) {
      this.snapshotValid.set(false);
      this.errorState.set('Refresh the device list before selecting a device.');
      return;
    }
    this.refreshRequest?.unsubscribe();
    this.selectedSerialState.set(serial);
    this.snapshotValid.set(false);
    this.errorState.set(null);
    this.selectionFailed.set(false);
    this.selectionPending.set(true);
    let succeeded = false;
    this.selectionRequest = this.http.post<DeviceSelectionResponse>(
      '/api/devices/selection', { serial, expected_adb_endpoint_id: this.adbEndpointId() }
    ).pipe(
      timeout(10000),
      finalize(() => {
        this.selectionPending.set(false);
        this.selectionRequest = null;
        if (succeeded && this.watcherCount > 0) {
          this.refresh();
        }
      })
    ).subscribe({
      next: (response) => {
        this.selectedSerialState.set(response.selected_serial);
        succeeded = true;
      },
      error: (error: HttpErrorResponse) => {
        this.selectionFailed.set(true);
        this.errorState.set(this.errorMessage(error, 'Could not select this device. Choose a device again.'));
      }
    });
  }

  private errorMessage(error: HttpErrorResponse, fallback: string): string {
    const detail = error.error?.detail;
    return typeof detail === 'string' && detail.trim() ? detail : fallback;
  }

  private stopPolling(): void {
    if (this.pollTimer !== null) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
    this.refreshRequest?.unsubscribe();
    this.snapshotValid.set(false);
  }
}
