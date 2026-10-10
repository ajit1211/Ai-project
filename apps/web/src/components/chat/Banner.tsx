/** A dismissible error banner (the shared Alert plus Dismiss and an optional Try again). */

import { Alert } from "../forms";
import { Icon } from "./Icon";

export interface BannerProps {
  message: string;
  onDismiss: () => void;
  onRetry?: () => void;
}

export function Banner({ message, onDismiss, onRetry }: BannerProps) {
  return (
    <div className="chat-banner">
      <Alert kind="error">{message}</Alert>
      <div className="chat-banner-actions">
        {onRetry && (
          <button type="button" className="btn btn-quiet" onClick={onRetry}>
            Try again
          </button>
        )}
        <button type="button" className="icon-button" aria-label="Dismiss" onClick={onDismiss}>
          <Icon name="close" />
        </button>
      </div>
    </div>
  );
}
