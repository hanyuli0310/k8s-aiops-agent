/** PermissionCard：治理动作确认卡片（阻塞式确认，方案 A） */
import { useEffect, useState } from 'react';
import { Button, Checkbox, Tag } from 'antd';
import { ExclamationCircleFilled, SafetyCertificateOutlined } from '@ant-design/icons';
import type { PermissionCardProps } from './types';
import styles from './index.module.css';

export default function PermissionCard({
  tool,
  summary,
  args,
  isDestructive,
  timeoutS,
  createdAt,
  expiring,
  onRespond,
}: PermissionCardProps): JSX.Element {
  const [remember, setRemember] = useState<boolean>(false);
  const [left, setLeft] = useState<number>(timeoutS);

  // 本地倒计时：后端超时按拒绝处理，让用户看得见剩余时间
  useEffect(() => {
    const tick = (): void => {
      const elapsed = (Date.now() - createdAt) / 1000;
      setLeft(Math.max(0, Math.round(timeoutS - elapsed)));
    };
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [createdAt, timeoutS]);

  const argEntries = Object.entries(args).filter(([, v]) => v !== null && v !== undefined);

  return (
    <div className={`${styles.card} ${expiring ? styles.cardExpiring : ''}`}>
      <div className={styles.head}>
        {isDestructive ? (
          <ExclamationCircleFilled className={styles.iconDanger} />
        ) : (
          <SafetyCertificateOutlined className={styles.iconNormal} />
        )}
        <span className={styles.title}>需要你确认才能执行</span>
        {isDestructive && <Tag color="red">变更集群</Tag>}
        <span className={styles.countdown}>
          {left > 0 ? `${left}s 后自动拒绝` : '已超时'}
        </span>
      </div>

      <div className={styles.summary}>{summary}</div>

      {argEntries.length > 0 && (
        <div className={styles.args}>
          {argEntries.map(([k, v]) => (
            <div className={styles.argRow} key={k}>
              <span className={styles.argKey}>{k}</span>
              <span className={styles.argVal}>
                {typeof v === 'object' ? JSON.stringify(v) : String(v)}
              </span>
            </div>
          ))}
        </div>
      )}

      <div className={styles.toolLine}>
        工具 <code className={styles.toolName}>{tool}</code>
      </div>

      <div className={styles.actions}>
        <Checkbox checked={remember} onChange={(e) => setRemember(e.target.checked)}>
          本次会话内同一资源不再询问
        </Checkbox>
        <div className={styles.buttons}>
          <Button size="small" onClick={() => onRespond(false, false)}>
            拒绝
          </Button>
          <Button
            size="small"
            type="primary"
            danger={isDestructive}
            onClick={() => onRespond(true, remember)}
          >
            批准执行
          </Button>
        </div>
      </div>
    </div>
  );
}
