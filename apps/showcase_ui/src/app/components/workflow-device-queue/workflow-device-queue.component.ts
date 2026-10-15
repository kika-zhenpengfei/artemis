import { ChangeDetectionStrategy, Component, DestroyRef, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { DeviceSelectionService, SelectableDevice } from '../../services/device-selection.service';
import { WorkflowDeviceService } from '../../services/workflow-device.service';

@Component({
  selector: 'app-workflow-device-queue',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './workflow-device-queue.component.html',
  styleUrl: './workflow-device-queue.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class WorkflowDeviceQueueComponent implements OnInit {
  public readonly workflowDevices = inject(WorkflowDeviceService);
  public readonly deviceSelection = inject(DeviceSelectionService);
  private readonly destroyRef = inject(DestroyRef);

  public ngOnInit(): void {
    this.destroyRef.onDestroy(this.deviceSelection.watchDevices());
    this.destroyRef.onDestroy(this.workflowDevices.watchWorkflows());
  }

  public refresh(): void {
    this.deviceSelection.refresh();
    this.workflowDevices.refresh();
  }

  public hasDevice(serial: string): boolean {
    return this.deviceSelection.devices().some(device => device.serial === serial);
  }

  public deviceDisabled(device: SelectableDevice): boolean {
    return device.state !== 'device' || device.is_busy || device.queued_count > 0;
  }

  public deviceLabel(device: SelectableDevice): string {
    const model = device.model?.replace(/_/g, ' ');
    const name = model ? `${model} · ${device.serial}` : device.serial;
    const status = device.state === 'unauthorized' ? 'Unauthorized'
      : device.state !== 'device' ? 'Offline'
      : device.is_busy ? 'Busy' : device.queued_count > 0 ? 'Queued' : 'Idle';
    return `${name} · ${status}`;
  }
}
