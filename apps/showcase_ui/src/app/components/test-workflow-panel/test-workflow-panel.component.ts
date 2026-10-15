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

import { ChangeDetectionStrategy, Component, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { AgentService } from '../../services/agent.service';

@Component({
  selector: 'app-test-workflow-panel',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './test-workflow-panel.component.html',
  styleUrl: './test-workflow-panel.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class TestWorkflowPanelComponent implements OnInit {
  public agentService = inject(AgentService);

  public ngOnInit(): void {
    const sessionId = this.agentService.currentSessionId();
    if (sessionId) {
      this.agentService.fetchTestWorkflow(sessionId);
    }
  }

}
