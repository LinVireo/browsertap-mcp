// One operation holds the parent and any OOPIF debugger leases until cleanup.
// A frame is selected through its DOM owner, never by URL, title or inventory order.
globalThis.BtapFrameLocator = (() => {
  async function run(msg, sender, api) {
    const leases = [];
    const documents = [];
    const frames = [];
    const inputs = [];
    const group = `btap-frame-${crypto.randomUUID()}`;
    const deadline = Math.min(
      Number(msg.deadlineEpochMs) || Infinity,
      Date.now() + Math.min(Number(msg.timeoutMs) || 15000, 120000),
    );
    let root;
    let current;
    let element;
    let info;
    let topPoint;
    let snapshot = {};
    const remaining = () => {
      const value = Math.floor(deadline - Date.now());
      if (value <= 0) {
        const error = new Error('iframe operation deadline exhausted');
        error.code = 'cdp_timeout';
        throw error;
      }
      return value;
    };
    const refuse = detail => {
      const error = new Error(detail.status || 'frame locator failed');
      error.locator = detail;
      throw error;
    };
    const send = (lease, method, params = {}, state = null) =>
      api.send(lease, method, params, remaining(), 1, state);
    const attach = async target => {
      const lease = await api.attach(target, remaining());
      leases.push(lease);
      remaining();
      return lease;
    };
    const value = result => {
      if (result?.exceptionDetails) {
        const error = new Error('frame evaluation failed');
        error.code = 'frame_evaluation_failed';
        throw error;
      }
      return result?.result?.value;
    };
    const call = (ref, functionDeclaration, args = [], byValue = true) => send(
      ref.lease, 'Runtime.callFunctionOn', {
        objectId: ref.objectId, functionDeclaration,
        arguments: args.map(item => ({ value: item })),
        returnByValue: byValue, objectGroup: group,
      },
    );
    const inspect = async (ref, script) => value(await call(
      ref, `function() { return (${script}); }`,
    ));
    const select = async (doc, script, stage) => {
      const result = await call(doc, `function() {
        if (this !== document) return {found:false, status:'stale_frame'};
        return (${script});
      }`, [], false);
      value(result);
      if (result?.result?.subtype === 'node' && result.result.objectId)
        return { lease: doc.lease, objectId: result.result.objectId };
      const detail = result?.result?.objectId
        ? value(await call({ lease: doc.lease, objectId: result.result.objectId }, 'function() { return this; }'))
        : result?.result?.value;
      // A miss in a retired document is not evidence that the live locator is
      // gone. Check the already-bound path before returning a selection miss.
      await validateChain();
      refuse({ found: false, status: 'not_found', stage, ...detail });
    };
    const documentIn = async (lease, frameId) => {
      const world = await send(lease, 'Page.createIsolatedWorld', {
        frameId, worldName: 'browsertap-frame-locator',
      });
      if (!Number.isInteger(world.executionContextId)) throw new Error('frame context was not returned');
      const result = await send(lease, 'Runtime.evaluate', {
        expression: 'document', contextId: world.executionContextId, objectGroup: group,
      });
      value(result);
      if (!result?.result?.objectId) throw new Error('frame document was not returned');
      const doc = { lease, objectId: result.result.objectId, frameId };
      documents.push(doc);
      return doc;
    };
    const validateChain = async (mode = 'identity') => {
      for (const doc of documents) {
        if (value(await call(doc, 'function() { return this === document && !!this.defaultView; }')) !== true)
          refuse({ found: false, status: 'stale_frame', stage: 'document' });
      }
      let point = topPoint ? { ...topPoint.local } : null;
      for (const frame of [...frames].reverse()) {
        const described = await send(frame.lease, 'DOM.describeNode', { objectId: frame.objectId, depth: 0 });
        if (described.node?.frameId !== frame.frameId)
          refuse({ found: false, status: 'stale_frame', stage: 'frame' });
        const checked = value(await call(frame, msg.frameGuard, [mode, point?.x, point?.y]));
        if (!checked?.found) refuse(checked || { found: false, status: 'stale_frame', stage: 'frame' });
        if (point && (mode === 'click' || mode === 'point')) point = { x: checked.x, y: checked.y };
      }
      return point;
    };
    const finish = detail => {
      if (msg.action === 'query') return { ok: true, data: {
        ...snapshot,
        met: msg.gone ? detail.status === 'not_found' : detail.found === true,
        locator_status: detail.status, matches: detail.matches, stage: detail.stage,
      } };
      return { ok: true, data: {
        ...detail, input_dispatched: inputs.some(item => item.dispatched),
        input_commands_dispatched: inputs.filter(item => item.dispatched).length,
      } };
    };
    try {
      if (!['query', 'click', 'type'].includes(msg.action) ||
          !Array.isArray(msg.frameSelectors) || !msg.frameSelectors.length ||
          msg.frameSelectors.some(script => typeof script !== 'string'))
        return finish({ found: false, status: 'invalid_frame_locator' });
      const tabId = msg.tabId ?? sender.tab?.id;
      if (!Number.isInteger(tabId)) return finish({ found: false, status: 'invalid_target' });
      const lease = await attach({ tabId });
      const tree = await send(lease, 'Page.getFrameTree');
      root = await documentIn(lease, tree.frameTree.frame.id);
      current = root;
      snapshot = value(await call(root, 'function() { return {url:location.href, title:document.title, ready:document.readyState}; }'));
      for (const script of msg.frameSelectors) {
        const owner = await select(current, script, 'frame');
        const described = await send(owner.lease, 'DOM.describeNode', { objectId: owner.objectId, depth: 0 });
        const frameId = described.node?.frameId;
        if (!frameId || !['IFRAME', 'FRAME'].includes(described.node?.nodeName))
          refuse({ found: false, status: 'not_a_frame', stage: 'frame' });
        frames.push({ ...owner, frameId });
        // Same-process frames (including cross-origin ones) have contexts in
        // this target. An OOPIF instead has its own target with the exact DOM
        // frameId. Only this explicit, read-only miss permits the second route.
        try {
          current = await documentIn(current.lease, frameId);
        } catch (error) {
          if (!/no frame|frame.*not found|frame.*does not belong/i.test(String(error.message))) throw error;
          const childLease = await attach({ targetId: frameId });
          const childTree = await send(childLease, 'Page.getFrameTree');
          if (childTree.frameTree?.frame?.id !== frameId)
            refuse({ found: false, status: 'stale_frame', stage: 'frame' });
          current = await documentIn(childLease, frameId);
        }
      }
      await validateChain();
      element = msg.point ? current : await select(current, msg.nodeSelector, 'element');
      if (msg.action === 'query') {
        info = await inspect(element, msg.inspect);
        await validateChain();
        return finish(info);
      }

      // Keep setup, renderer round trips, guards and input inside these same
      // attached leases. No detach/re-attach gap and no mutation retry.
      for (const held of leases) await send(held, 'Emulation.setFocusEmulationEnabled', { enabled: true });
      if (value(await call(root, 'function() { return document.hasFocus(); }')) !== true)
        refuse({ found: false, status: 'focus_failed', stage: 'document' });
      info = msg.point ? { found: true, status: 'found', ...msg.point, width: 0, height: 0 } : await inspect(element, msg.inspect);
      if (!info?.found) refuse(info || { found: false, status: 'not_found' });
      const beforeMarker = info.challengeMarker || null;
      if (beforeMarker && beforeMarker === msg.blockedMarker)
        return finish({ ...info, status: 'challenge_stalled', beforeMarker });
      if (msg.action === 'click') {
        topPoint = { local: {
          x: info.x + (msg.centerX ? info.width / 2 : 0),
          y: info.y + (msg.centerY ? info.height / 2 : 0),
        } };
        if (!Number.isFinite(topPoint.local.x) || !Number.isFinite(topPoint.local.y))
          refuse({ found: false, status: 'invalid_geometry' });
        topPoint.global = await validateChain(msg.point ? 'point' : 'click');
      } else {
        await validateChain('focus');
      }
      const results = [];
      for (const command of msg.commands || []) {
        if (!String(command.method).startsWith('Input.'))
          refuse({ found: false, status: 'invalid_input_command' });
        if (info.targetKind === 'xterm' && command.method === 'Input.dispatchKeyEvent' &&
            results.length === 1 && msg.submitDelayMs) {
          if (remaining() <= msg.submitDelayMs) {
            const error = new Error('iframe operation deadline cannot fit submit delay');
            error.code = 'cdp_timeout';
            throw error;
          }
          await new Promise(resolve => setTimeout(resolve, msg.submitDelayMs));
        }
        // Delays precede the guards: navigation or focus loss while waiting
        // must stop Enter just as it stops any other input in the sequence.
        if (msg.action === 'click') {
          if (!msg.point) {
            const latest = await inspect(element, msg.inspect);
            if (!latest?.found) refuse(latest || { found: false, status: 'stale_frame' });
            if (latest.x !== info.x || latest.y !== info.y || latest.width !== info.width || latest.height !== info.height)
              refuse({ found: false, status: 'stale_frame', stage: 'geometry' });
          }
          const point = await validateChain(msg.point ? 'point' : 'click');
          if (point.x !== topPoint.global.x || point.y !== topPoint.global.y)
            refuse({ found: false, status: 'stale_frame', stage: 'geometry' });
        } else {
          await validateChain('focus');
          const focused = value(await call(element, `function() {
            let active = document.activeElement;
            while (active && active.shadowRoot && active.shadowRoot.activeElement) active = active.shadowRoot.activeElement;
            return this.isConnected && this.ownerDocument === document && active === this &&
              !this.disabled && !this.readOnly && this.getAttribute('aria-disabled') !== 'true' && document.hasFocus();
          }`));
          if (focused !== true) refuse({ found: false, status: 'focus_failed', stage: 'element' });
        }
        const params = { ...command.params };
        if (msg.action === 'click') Object.assign(params, topPoint.global);
        const state = { dispatched: false };
        inputs.push(state);
        results.push(await send(msg.action === 'click' ? root.lease : current.lease, command.method, params, state));
        state.dispatched = true;
      }
      const detail = {
        ...info, status: 'success', result: results, beforeMarker,
        ...(topPoint ? topPoint.global : {}),
        hitVerified: msg.action === 'click' && !msg.point && info.hitVerified === true,
      };
      if (msg.action === 'click') {
        try {
          const after = msg.point ? null : await inspect(element, msg.observe);
          detail.afterMarker = after?.found ? after.challengeMarker || null : null;
          detail.challenge_check = { enforced: Boolean(after?.found) };
        } catch (_) {
          detail.challenge_check = { enforced: false, reason: 'probe_unavailable' };
        }
      }
      return finish(detail);
    } catch (error) {
      if (error.locator) return finish(error.locator);
      if (/cannot find context|cannot find object|could not find object|execution context was destroyed|no target with given id/i.test(String(error.message)))
        return finish({ found: false, status: 'stale_frame', stage: 'document' });
      const dispatched = inputs.some(item => item.dispatched);
      const code = error.code || api.failureCode(error);
      return { ok: false, code, error: {
        code, message: error.message || String(error),
        input_dispatched: dispatched, dispatched, retryable: false,
        input_commands_dispatched: inputs.filter(item => item.dispatched).length,
      } };
    } finally {
      // Object groups belong to this operation, including when another BTAP
      // capture keeps an attachment alive after our leases are released.
      await Promise.all(leases.map(async lease => {
        try {
          await api.send(lease, 'Runtime.releaseObjectGroup', { objectGroup: group },
            Math.max(1, Math.min(100, deadline - Date.now())), 1);
        } catch (_) {}
      }));
      await Promise.all(leases.map(async lease => {
        try { await api.detach(lease); } catch (_) {}
      }));
    }
  }
  return { run };
})();
