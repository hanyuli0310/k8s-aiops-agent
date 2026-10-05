/** MermaidBlock：把 markdown 里的 ```mermaid 代码块渲染成图。

流式输出期间代码可能不完整（渲染必然失败），失败时降级为源码展示，
待流结束代码完整后自动重渲染成功。
*/
import { useEffect, useId, useRef, useState } from 'react';
import type { ComponentPropsWithoutRef } from 'react';
import mermaid from 'mermaid';
import type { MermaidBlockProps } from './types';
import styles from './index.module.css';

mermaid.initialize({
  startOnLoad: false,
  theme: 'neutral',
  securityLevel: 'loose',
  flowchart: { htmlLabels: true, curve: 'basis' },
});

export default function MermaidBlock({ code }: MermaidBlockProps): JSX.Element {
  const containerRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string>('');
  // useId 含冒号，mermaid 要求合法 CSS id，需清洗
  const renderId = `mmd${useId().replace(/[^a-zA-Z0-9]/g, '')}`;

  useEffect(() => {
    let cancelled = false;
    const render = async (): Promise<void> => {
      try {
        const { svg } = await mermaid.render(renderId, code.trim());
        if (!cancelled && containerRef.current) {
          containerRef.current.innerHTML = svg;
          setError('');
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
        }
        // mermaid.render 失败会在 body 残留占位节点，清理掉
        document.getElementById(renderId)?.remove();
      }
    };
    void render();
    return () => {
      cancelled = true;
    };
  }, [code, renderId]);

  if (error) {
    return (
      <div className={styles.wrap}>
        <div className={styles.errorTip}>mermaid 渲染失败（流式输出中代码可能尚不完整）</div>
        <pre className={styles.fallback}>{code}</pre>
      </div>
    );
  }
  return <div ref={containerRef} className={styles.wrap} />;
}

/** 供 ReactMarkdown components 复用：```mermaid 代码块转图，其余保持默认 */
export function MarkdownCode(props: ComponentPropsWithoutRef<'code'>): JSX.Element {
  const { className, children, ...rest } = props;
  if (/language-mermaid/.test(className ?? '')) {
    return <MermaidBlock code={String(children ?? '')} />;
  }
  return (
    <code className={className} {...rest}>
      {children}
    </code>
  );
}

export const markdownComponents = { code: MarkdownCode };
