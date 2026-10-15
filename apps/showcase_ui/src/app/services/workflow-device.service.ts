import { DestroyRef, Injectable, inject, signal } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Subscription, finalize, timeout } from 'rxjs';
import { DeviceSelectionService } from './device-selection.service';

export interface WaitingWorkflow {
  workflow_id: string;
  goal: string;
  profile: 'flash' | 'pro';
  case_count: number;
  status: 'awaiting_device' | 'confirmed' | 'starting';
  device_serial: string | null;
  adb_endpoint_id: string | null;
  error: string | null;
}

export interface WorkflowDeviceChoice {
  serial: string;
  endpointId: string | null;
  editing: boolean;
}

export type WorkflowDeviceConfirmation = Pick<WaitingWorkflow,
  'workflow_id' | 'status' | 'device_serial' | 'adb_endpoint_id' | 'error'>;

@Injectable({ providedIn: 'root' })
export class WorkflowDeviceService {
  private readonly http = inject(HttpClient);
  private readonly devices = inject(DeviceSelectionService);
  private readonly workflowState = signal<WaitingWorkflow[]>([]);
  private readonly choices = signal<Record<string, WorkflowDeviceChoice>>({});
  private readonly confirming = signal<Record<string, boolean>>({});
  private readonly confirmationErrors = signal<Record<string, string>>({});
  private readonly loadingState = signal(false);
  private readonly snapshotValid = signal(false);
  private readonly errorState = signal<string | null>(null);
  private refreshRequest: Subscription | null = null;
  private readonly confirmationRequests = new Map<string, Subscription>();
  private pollTimer: ReturnType<typeof setInterval> | null = null;
  private watcherCount = 0;

  public readonly workflows = this.workflowState.asReadonly();
  public readonly isLoading = this.loadingState.asReadonly();
  public readonly error = this.errorState.asReadonly();

  constructor() {
    inject(DestroyRef).onDestroy(() => {
      this.stopPolling();
      for (const request of this.confirmationRequests.values()) {
        request.unsubscribe();
      }
    });
  }

  public watchWorkflows(): () => void {
    this.watcherCount++;
    if (this.watcherCount === 1) {
      this.refresh();
      this.pollTimer = setInterval(() => this.refresh(), 3000);
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
    if (this.isLoading() || Object.values(this.confirming()).some(Boolean)) {
      return;
    }
    this.loadingState.set(true);
    this.refreshRequest = this.http.get<{ workflows: WaitingWorkflow[] }>('/api/test_workflows/waiting').pipe(
      timeout(10000),
      finalize(() => {
        this.loadingState.set(false);
        this.refreshRequest = null;
      })
    ).subscribe({
      next: response => {
        this.applyWorkflows(response.workflows);
        this.snapshotValid.set(true);
        this.errorState.set(null);
      },
      error: error => {
        this.snapshotValid.set(false);
        this.errorState.set(this.errorMessage(error, 'Could not refresh waiting workflows. Try again.'));
      }
    });
  }

  public choice(workflow: WaitingWorkflow): WorkflowDeviceChoice {
    return this.choices()[workflow.workflow_id] ?? {
      serial: workflow.device_serial || '', endpointId: workflow.adb_endpoint_id, editing: false
    };
  }

  public isEditing(workflow: WaitingWorkflow): boolean {
    return workflow.status === 'awaiting_device'
      || (workflow.status === 'confirmed' && this.choice(workflow).editing);
  }

  public isConfirming(workflowId: string): boolean {
    return !!this.confirming()[workflowId];
  }

  public errorFor(workflow: WaitingWorkflow): string | null {
    return this.confirmationErrors()[workflow.workflow_id] || workflow.error;
  }

  public chooseDevice(workflowId: string, serial: string): void {
    const workflow = this.workflows().find(item => item.workflow_id === workflowId);
    if (!workflow || !this.isEditing(workflow) || this.isConfirming(workflowId)) {
      return;
    }
    this.choices.update(choices => ({
      ...choices, [workflowId]: { serial, endpointId: this.devices.adbEndpointId(), editing: true }
    }));
    this.clearConfirmationError(workflowId);
  }

  public changeDevice(workflowId: string): void {
    const workflow = this.workflows().find(item => item.workflow_id === workflowId);
    if (!workflow || workflow.status === 'starting' || this.isConfirming(workflowId)) {
      return;
    }
    this.choices.update(choices => ({ ...choices, [workflowId]: { serial: '', endpointId: null, editing: true } }));
    this.clearConfirmationError(workflowId);
  }

  public selectionHint(workflow: WaitingWorkflow): string | null {
    if (!this.snapshotValid()) {
      return 'Refresh workflows before confirming a device.';
    }
    const endpointId = this.devices.adbEndpointId();
    if (!endpointId) {
      return 'Refresh devices before confirming.';
    }
    const choice = this.choice(workflow);
    if (!choice.serial) {
      return 'Choose an idle device.';
    }
    if (choice.endpointId !== endpointId) {
      return 'Device connection changed. Reset the choice and select again.';
    }
    const device = this.devices.devices().find(item => item.serial === choice.serial);
    if (!device || device.state !== 'device') {
      return 'This device is disconnected or not authorized.';
    }
    if (device.is_busy || device.queued_count > 0) {
      return 'This device is busy or has queued tasks.';
    }
    return null;
  }

  public canConfirm(workflow: WaitingWorkflow): boolean {
    return this.isEditing(workflow) && !this.isConfirming(workflow.workflow_id)
      && !this.devices.isLoading() && !this.selectionHint(workflow);
  }

  public confirm(workflowId: string): void {
    const workflow = this.workflows().find(item => item.workflow_id === workflowId);
    if (!workflow || !this.canConfirm(workflow)) {
      return;
    }
    const choice = this.choice(workflow);
    // A list response captured before this confirmation must not undo it.
    this.refreshRequest?.unsubscribe();
    this.confirming.update(state => ({ ...state, [workflowId]: true }));
    this.clearConfirmationError(workflowId);
    const request = this.http.post<WorkflowDeviceConfirmation>(
      `/api/test_workflows/${encodeURIComponent(workflowId)}/confirm_device`,
      { device_serial: choice.serial, expected_adb_endpoint_id: choice.endpointId }
    ).pipe(
      timeout(10000),
      finalize(() => {
        this.confirming.update(state => ({ ...state, [workflowId]: false }));
        this.confirmationRequests.delete(workflowId);
      })
    ).subscribe({
      next: confirmed => this.applyWorkflows(this.workflows().map(item =>
        item.workflow_id === workflowId ? { ...item, ...confirmed } : item
      )),
      error: error => this.confirmationErrors.update(errors => ({
        ...errors, [workflowId]: this.errorMessage(error, 'Could not confirm this device. Refresh or try again.')
      }))
    });
    this.confirmationRequests.set(workflowId, request);
  }

  private applyWorkflows(workflows: WaitingWorkflow[]): void {
    const choices: Record<string, WorkflowDeviceChoice> = {};
    const errors: Record<string, string> = {};
    for (const workflow of workflows) {
      const id = workflow.workflow_id;
      const choice = this.choices()[id];
      const confirmationMatches = workflow.status === 'confirmed' && choice?.serial === workflow.device_serial
        && choice.endpointId === workflow.adb_endpoint_id;
      if (workflow.status === 'starting' || confirmationMatches || (workflow.status === 'confirmed' && !choice?.editing)) {
        choices[id] = { serial: workflow.device_serial || '', endpointId: workflow.adb_endpoint_id, editing: false };
      } else if (choice) {
        choices[id] = choice;
      }
      if (this.confirmationErrors()[id] && workflow.status !== 'starting' && !confirmationMatches) {
        errors[id] = this.confirmationErrors()[id];
      }
    }
    this.workflowState.set(workflows);
    this.choices.set(choices);
    this.confirmationErrors.set(errors);
  }

  private clearConfirmationError(workflowId: string): void {
    this.confirmationErrors.update(errors => {
      const next = { ...errors };
      delete next[workflowId];
      return next;
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
