export const forfeitViewTemplate = `
<section class="panel">
  <h2>罚没册</h2>
  <p class="hint">仅核验违约的笼可罚没。资金离开当事人账户，进入罚没册，不再回到可用余额。</p>
  <div v-if="forfeitFate" class="proof-box" style="margin-bottom:12px">
    双扣命运纪要 · 口径 {{ forfeitFate.caliber }}（{{ forfeitFate.status }}）：{{ forfeitFate.rule }}
  </div>
  <div class="grid-2">
    <div>
      <div class="form-row">
        <div>
          <label>违约态笼</label>
          <select v-model.number="forfeitForm.cage_id">
            <option :value="0">请选择</option>
            <option v-for="c in failCages" :key="c.id" :value="c.id">
              {{ c.cage_code }} · 可用 {{ fmt(c.available) }}
            </option>
          </select>
        </div>
        <div>
          <label>金额（空=全额）</label>
          <input type="number" v-model.number="forfeitForm.amount" min="0" step="1000" placeholder="全额"/>
        </div>
        <div style="grid-column:1/-1">
          <label>原因</label>
          <input v-model="forfeitForm.reason"/>
        </div>
      </div>
      <div class="actions">
        <button class="danger" @click="doForfeit">执行罚没</button>
      </div>
      <h3 style="margin-top:14px">罚没双扣偏航记（正式零改拒写留痕）</h3>
      <table>
        <thead>
          <tr><th>时间</th><th>笼</th><th>尝试额</th><th>在手冻结</th><th>应冻</th><th>经办</th><th>原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="y in forfeitYaw" :key="y.id">
            <td>{{ y.created_at }}</td>
            <td>{{ y.cage_code }}</td>
            <td>{{ fmt(y.attempt_amount) }}</td>
            <td><span class="badge fail">{{ fmt(y.frozen_on_hand) }}</span></td>
            <td>{{ fmt(y.needed_frozen) }}</td>
            <td>{{ y.actor }}</td>
            <td>{{ y.reason }}</td>
          </tr>
          <tr v-if="!forfeitYaw.length"><td colspan="7" class="hint">暂无偏航记录</td></tr>
        </tbody>
      </table>
    </div>
    <div>
      <table>
        <thead>
          <tr><th>凭证</th><th>笼</th><th>金额</th><th>原因</th><th>笼代</th><th>双扣</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="f in forfeits" :key="f.id">
            <td>{{ f.forfeit_ref }}</td>
            <td>{{ f.cage_code }}</td>
            <td>{{ fmt(f.amount) }}</td>
            <td>{{ f.reason }}</td>
            <td>{{ f.cage_generation }}</td>
            <td><span class="badge pass">余额/冻结同额</span></td>
            <td>{{ f.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</section>
`;
