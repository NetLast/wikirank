diff --git a/README.md b/README.md
index 8d3d203..1379264 100644
--- a/README.md
+++ b/README.md
@@ -13,6 +13,12 @@
   автоматично показує кількісний
 - **Шаблон конкурсу**: вставте посилання на шаблон — інструмент знайде всі
   статті кампанії та автоматично витягне список учасників у межах дат
+- **Мінімум байтів для доповнення**: адмін задає в налаштуваннях конкурсу, скільки
+  байтів має додати учасник, щоб доповнення статті потрапило на перевірку журі
+  (нові статті не відсіюються; 0 = вимкнено)
+- **Рейтинг за балами журі**: таблиця сортується за середнім балом окремо за нові та
+  окремо за доповнені статті (клік по заголовку, діє й на CSV), а блок «Найвищі бали журі»
+  показує топ статей двома стовпцями з стовпчиками
 - **Коментарі журі** зберігаються з автором і датою
 - Список учасників із посиланнями на внесок **у новій вкладці**
 - **Експорт CSV** для протоколу журі (з BOM, відкривається в Excel)
diff --git a/app.py b/app.py
index cda2303..44a4b1c 100644
--- a/app.py
+++ b/app.py
@@ -84,6 +84,10 @@ CREATE TABLE IF NOT EXISTS contest_jury(
   contest_id TEXT, login TEXT, PRIMARY KEY(contest_id, login));
 """
 
+# раніше мінімальний внесок учасника був зашитий (1000 Б) для всіх конкурсів;
+# тепер це налаштування конкурсу, а значення нижче лише зберігає поведінку вже наявних конкурсів
+LEGACY_MIN_BYTES = 1000
+
 def init_db():
     con = sqlite3.connect(DB_PATH)
     had_reviews = con.execute(
@@ -100,6 +104,17 @@ def init_db():
         con.execute("ALTER TABLE contests ADD COLUMN tpl_talk_only INTEGER DEFAULT 0")
     if "review_scope" not in cols:
         con.execute("ALTER TABLE contests ADD COLUMN review_scope TEXT DEFAULT 'all'")
+    if "min_expand" not in cols:
+        # мінімум доданих байтів, щоб доповнення статті потрапило на перевірку журі (0 = без обмеження)
+        con.execute("ALTER TABLE contests ADD COLUMN min_expand INTEGER DEFAULT 0")
+    if "min_bytes" not in cols:
+        # мінімальний внесок учасника (сума доданих байтів); 0 = без обмеження
+        con.execute("ALTER TABLE contests ADD COLUMN min_bytes INTEGER DEFAULT 0")
+        # наявні конкурси мали поріг 1000 Б — зберігаємо їхню поведінку, адмін може змінити
+        con.execute("UPDATE contests SET min_bytes=?", (LEGACY_MIN_BYTES,))
+    if "min_created" not in cols:
+        # мінімум байтів для нової (започаткованої) статті (0 = без обмеження)
+        con.execute("ALTER TABLE contests ADD COLUMN min_created INTEGER DEFAULT 0")
     if not had_cj:
         # раніше журі було спільним для всіх конкурсів: щоб не порушити перевірки, що вже тривають,
         # додаємо всіх наявних журі до всіх наявних конкурсів (адмін зможе звузити склад)
@@ -120,8 +135,6 @@ _OPS = {ast.Add: operator.add, ast.Sub: operator.sub,
         ast.USub: operator.neg, ast.UAdd: operator.pos,
         ast.Pow: operator.pow}
 FORMULA_VARS = ("bytes", "edits", "articles", "quality", "created", "expanded")
-# мінімальний внесок: учасники з меншою сумою доданих байтів не потрапляють у рейтинг
-MIN_BYTES = 1000
 
 def eval_formula(expr, variables):
     def _ev(node):
@@ -553,6 +566,83 @@ _NEW = {
 for _l, _d in _NEW.items():
     T[_l].update(_d)
 
+_RANK = {
+ "uk": dict(
+   min_expand="Мінімум байтів для доповненої статті",
+   tip_min_expand="Доповнення, де учасник додав менше байтів, не потрапляють на перевірку журі. 0 = без обмеження",
+   min_expand_h="Доповнення з меншим приростом відсіюються з розподілу, переліку статей, прогресу й якості (так само, як учасники нижче мінімального внеску). Нові статті не відсіюються; кількісний внесок не змінюється. 0 = вимкнено.",
+   min_expand_chip="Мін. доповнення:", bytes_unit="Б",
+   min_bytes="Мінімальний внесок учасника (байтів)",
+   tip_min_bytes="Учасники, які додали за період менше байтів, не потрапляють у рейтинг і на перевірку. 0 = без обмеження",
+   min_bytes_h="Сума доданих байтів учасника. Менший внесок — учасника приховано з таблиці, розподілу й експорту. 0 = вимкнено.",
+   min_bytes_chip="Мін. внесок:",
+   min_created="Мінімум байтів для нової статті",
+   tip_min_created="Нові (започатковані) статті, у яких менше байтів, не потрапляють на перевірку журі. 0 = без обмеження",
+   min_created_h="Працює так само, як поріг для доповнень, але для нових статей (за правилами, напр., 3500 Б). Рахуються додані учасником байти.",
+   min_created_chip="Мін. нова стаття:",
+   hidden_exp="Приховано статей, менших за мінімум (нові + доповнення)",
+   th_avg_c="Бал: нові", th_avg_e="Бал: доповнені",
+   tip_th_avg_c="Середній бал журі за нові статті учасника. Клікніть, щоб відсортувати",
+   tip_th_avg_e="Середній бал журі за доповнені статті учасника. Клікніть, щоб відсортувати",
+   top_title="Найвищі бали журі", top_created="Нові статті", top_expanded="Доповнені статті",
+   top_show="Показати", top_none="Оцінених статей ще немає",
+   top_h="Рейтинг статей за середнім балом журі — окремо створені та окремо доповнені.",
+   me_warn="⚠ Після збереження з перевірки випаде оцінених статей: {n}. Оцінки лишаться в базі, але не враховуватимуться в якості й балах.",
+   me_ok="Оцінених статей, що випадуть, немає.",
+   me_confirm="Відсіяти оцінені статті ({n})? Їхні оцінки більше не враховуватимуться в якості й балах (у базі вони лишаться).",
+   me_more="…та ще"),
+ "en": dict(
+   min_expand="Minimum bytes for an expanded article",
+   tip_min_expand="Expansions where the participant added fewer bytes are not sent to the jury. 0 = no limit",
+   min_expand_h="Expansions below the threshold are dropped from distribution, article lists, progress and quality (the same way participants below the minimum contribution are). New articles are never dropped; quantitative contribution is unchanged. 0 = off.",
+   min_expand_chip="Min. expansion:", bytes_unit="B",
+   min_bytes="Minimum participant contribution (bytes)",
+   tip_min_bytes="Participants who added fewer bytes in the period are left out of the ranking and review. 0 = no limit",
+   min_bytes_h="The participant's total added bytes. Smaller contributions are hidden from the table, distribution and export. 0 = off.",
+   min_bytes_chip="Min. contribution:",
+   min_created="Minimum bytes for a new article",
+   tip_min_created="New (started) articles with fewer bytes are not sent to the jury. 0 = no limit",
+   min_created_h="Works like the expansion threshold, but for new articles (e.g. 3500 B per the rules). Counts the bytes added by the participant.",
+   min_created_chip="Min. new article:",
+   hidden_exp="Articles hidden for being below the minimum (new + expansions)",
+   th_avg_c="Score: new", th_avg_e="Score: expanded",
+   tip_th_avg_c="Average jury score for the participant's new articles. Click to sort",
+   tip_th_avg_e="Average jury score for the participant's expanded articles. Click to sort",
+   top_title="Top jury scores", top_created="New articles", top_expanded="Expanded articles",
+   top_show="Show", top_none="No scored articles yet",
+   top_h="Articles ranked by average jury score — new and expanded separately.",
+   me_warn="⚠ After saving, scored articles that will drop out of the review: {n}. The scores stay in the database but will not count toward quality and scores.",
+   me_ok="No scored articles will drop out.",
+   me_confirm="Drop scored articles ({n})? Their scores will stop counting toward quality and scores (they stay in the database).",
+   me_more="…and more:"),
+ "pl": dict(
+   min_expand="Minimum bajtów dla rozbudowanego artykułu",
+   tip_min_expand="Rozbudowy, w których uczestnik dodał mniej bajtów, nie trafiają do oceny jury. 0 = bez limitu",
+   min_expand_h="Rozbudowy poniżej progu są pomijane w podziale, liście artykułów, postępie i jakości (tak jak uczestnicy poniżej minimalnego wkładu). Nowe artykuły nie są pomijane; wkład ilościowy się nie zmienia. 0 = wyłączone.",
+   min_expand_chip="Min. rozbudowa:", bytes_unit="B",
+   min_bytes="Minimalny wkład uczestnika (bajty)",
+   tip_min_bytes="Uczestnicy, którzy dodali mniej bajtów w okresie, nie trafiają do rankingu ani oceny. 0 = bez limitu",
+   min_bytes_h="Suma dodanych bajtów uczestnika. Mniejszy wkład — uczestnik ukryty w tabeli, podziale i eksporcie. 0 = wyłączone.",
+   min_bytes_chip="Min. wkład:",
+   min_created="Minimum bajtów dla nowego artykułu",
+   tip_min_created="Nowe artykuły z mniejszą liczbą bajtów nie trafiają do oceny jury. 0 = bez limitu",
+   min_created_h="Działa jak próg dla rozbudów, ale dla nowych artykułów (np. 3500 B wg regulaminu). Liczą się bajty dodane przez uczestnika.",
+   min_created_chip="Min. nowy artykuł:",
+   hidden_exp="Ukryte artykuły poniżej minimum (nowe + rozbudowy)",
+   th_avg_c="Ocena: nowe", th_avg_e="Ocena: rozbudowane",
+   tip_th_avg_c="Średnia ocena jury za nowe artykuły uczestnika. Kliknij, aby sortować",
+   tip_th_avg_e="Średnia ocena jury za rozbudowane artykuły uczestnika. Kliknij, aby sortować",
+   top_title="Najwyższe oceny jury", top_created="Nowe artykuły", top_expanded="Rozbudowane artykuły",
+   top_show="Pokaż", top_none="Brak ocenionych artykułów",
+   top_h="Artykuły według średniej oceny jury — osobno nowe i rozbudowane.",
+   me_warn="⚠ Po zapisaniu z oceny wypadną ocenione artykuły: {n}. Oceny zostaną w bazie, ale nie będą liczone w jakości i wynikach.",
+   me_ok="Żadne ocenione artykuły nie wypadną.",
+   me_confirm="Pominąć ocenione artykuły ({n})? Ich oceny przestaną się liczyć w jakości i wynikach (zostaną w bazie).",
+   me_more="…i jeszcze:"),
+}
+for _l, _d in _RANK.items():
+    T[_l].update(_d)
+
 def lang():
     l = request.args.get("lang") or session.get("lang") or "uk"
     if l not in T:
@@ -941,9 +1031,62 @@ let SCROLL_RESET=false;
 let MTAB='article';   // 'preview' | 'article' | 'history'
 const JURY_LIST={{juryjson|safe}};
 const SCOPE={{((contest.review_scope or 'all') if contest else 'all')|tojson}};
-const MIN_BYTES={{min_bytes}}; // мінімальний внесок для участі в рейтингу
+const MIN_BYTES={{((contest.min_bytes or 0) if contest else 0)|int}}; // мінімальний внесок учасника (налаштування конкурсу; 0 = вимкнено)
+const MIN_EXP={{((contest.min_expand or 0) if contest else 0)|int}}; // мінімум доданих байтів, щоб доповнення потрапило на перевірку
+const MIN_NEW={{((contest.min_created or 0) if contest else 0)|int}}; // мінімум байтів для нової статті
+let SORT_KEY=null; // null (за замовчуванням) | 'qc' (бал нових) | 'qe' (бал доповнених)
+let TOPN=10;
+const BU=LANG==='uk'?' Б':' B';
 const LEGACY={{ljson|safe}}; // оцінки/коментарі з попередньої версії (рівень учасника)
-function inScope(it){return SCOPE==='all'||(SCOPE==='created')===!!it.created}
+function inScope(it){
+ if((it.bytes||0)<(it.created?MIN_NEW:MIN_EXP))return false;
+ return SCOPE==='all'||(SCOPE==='created')===!!it.created}
+function setSort(k){SORT_KEY=SORT_KEY===k?null:k;ARTOPEN=null;render()}
+function setTopN(n){TOPN=Number(n)||10;render()}
+// середній бал журі окремо для нових і доповнених статей (у межах обсягу перевірки)
+function kindQuality(r,user){
+ const acc={created:[],expanded:[]};const seen=new Set();
+ if(r)Object.values(r.perProject||{}).forEach(pp=>(pp.list||[]).forEach(it=>{
+  if(!inScope(it)||seen.has(it.title))return;seen.add(it.title);
+  const vals=Object.values(slot(user,it.title).scores||{}).filter(v=>v!=null);
+  if(vals.length)acc[it.created?'created':'expanded'].push(vals.reduce((x,y)=>x+y,0)/vals.length)}));
+ const m=a=>a.length?a.reduce((x,y)=>x+y,0)/a.length:null;
+ return{qc:m(acc.created),qcn:acc.created.length,qe:m(acc.expanded),qen:acc.expanded.length};
+}
+// рейтинг статей за середнім балом журі: окремо створені й окремо доповнені
+function topArticles(rows){
+ const out={created:[],expanded:[]};
+ rows.forEach(row=>projects.forEach(p=>{
+  const pp=row.r?row.r.perProject[p.host]:null;
+  (pp&&pp.list?pp.list:[]).filter(inScope).forEach(it=>{
+   const vals=Object.values(slot(row.user,it.title).scores||{}).filter(v=>v!=null);
+   if(!vals.length)return;
+   out[it.created?'created':'expanded'].push({user:row.user,title:it.title,origin:p.origin,bytes:it.bytes,
+    avg:vals.reduce((x,y)=>x+y,0)/vals.length,n:vals.length});
+  })}));
+ const cmp=(a,b)=>(b.avg-a.avg)||(b.n-a.n)||(b.bytes-a.bytes);
+ out.created.sort(cmp);out.expanded.sort(cmp);return out;
+}
+function topHtml(rows){
+ const tp=topArticles(rows);
+ const mx=Math.max(1,...tp.created.concat(tp.expanded).map(x=>x.avg));
+ const col=(lbl,list,clr)=>{
+  let h='<div style="flex:1;min-width:300px"><div class="lbl" style="margin:0 0 6px;color:'+clr+'">'+esc(lbl)+' <span class="hint">('+list.length+')</span></div>';
+  if(!list.length)return h+'<div class="hint">'+esc(T.top_none)+'</div></div>';
+  list.slice(0,TOPN).forEach((x,i)=>{
+   h+='<div style="display:grid;grid-template-columns:22px 1fr 44px;gap:8px;align-items:center;padding:3px 0">'
+    +'<span class="mono hint">'+(i+1)+'</span><div style="min-width:0">'
+    +'<a class="wl" target="_blank" rel="noopener" href="'+articleLink(x.origin,x.title)+'" style="display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+esc(x.title)+'</a>'
+    +'<div class="hint" style="font-size:11px">'+esc(x.user)+' · +'+fmtN(x.bytes)+BU+' · '+x.n+' '+esc(T.scores_n)+'</div>'
+    +'<div class="bar"><i style="width:'+(x.avg/mx*100)+'%;background:'+clr+'"></i></div></div>'
+    +'<span class="mono" style="font-weight:700;text-align:right">'+x.avg.toFixed(2)+'</span></div>'});
+  return h+'</div>'};
+ let h='<div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:10px"><h3 style="margin:0">'+esc(T.top_title)+'</h3>'
+  +'<span class="hint">'+esc(T.top_h)+'</span><span style="margin-left:auto" class="hint">'+esc(T.top_show)+' <select onchange="setTopN(this.value)" style="width:auto;padding:2px 6px">'
+  +[5,10,25,50].map(n=>'<option value="'+n+'"'+(n===TOPN?' selected':'')+'>'+n+'</option>').join('')+'</select></span></div>';
+ h+='<div style="display:flex;flex-wrap:wrap;gap:24px">'+col(T.top_created,tp.created,'#1d7a4f')+col(T.top_expanded,tp.expanded,'var(--acc)')+'</div>';
+ return h;
+}
 function legacyDone(user){const l=LEGACY[user];return !!(l&&(Object.keys(l.scores||{}).length||(l.comments||[]).length))}
 function legacyVals(user){const l=LEGACY[user];return l?Object.values(l.scores||{}).filter(v=>v!=null):[]}
 function setArtFilter(f){ART_FILTER=f;SCROLL_RESET=true;render()}
@@ -1251,26 +1394,29 @@ function render(){
  const HAS_F=!!(CONTEST.formula&&CONTEST.formula.trim());
  const allRows=CONTEST.participants.map(user=>{
   const r=RESULTS?RESULTS[user]:null;const pa=ASSESS[user]||{};
-  const{quality,n}=articleQuality(pa,user,(SCOPE!=='all'&&r)?new Set(partTitles(r)):null);
-  const sp=splitStats(r);
+  const{quality,n}=articleQuality(pa,user,((SCOPE!=='all'||MIN_EXP>0||MIN_NEW>0)&&r)?new Set(partTitles(r)):null);
+  const sp=splitStats(r);const kq=kindQuality(r,user);
   const score=r?evalFormula(CONTEST.formula,{bytes:r.bytes,edits:r.edits,articles:r.articles,quality,created:sp.created,expanded:sp.expanded}):null;
   const titles=partTitles(r);
   const done=titles.filter(t=>isReviewed(user,t)).length;
   const mineT=titles.filter(t=>isMine(user,t));
   const mineDone=mineT.filter(t=>isReviewed(user,t)).length;
-  return{user,r,pa,quality,n,score,sp,total:IS_JURY?mineT.length:titles.length,done:IS_JURY?mineDone:done,mine:mineT.length,mineDone};
- }).sort((x,y)=>(QUAL&&!IS_JURY&&HAS_F)?((y.score??-1e18)-(x.score??-1e18)):((y.r?y.r.bytes:-1)-(x.r?x.r.bytes:-1)));
+  return{user,r,pa,quality,n,score,sp,kq,total:IS_JURY?mineT.length:titles.length,done:IS_JURY?mineDone:done,mine:mineT.length,mineDone};
+ }).sort((x,y)=>{
+  if(QUAL&&SORT_KEY){const a=x.kq[SORT_KEY]??-1e18,b=y.kq[SORT_KEY]??-1e18;if(a!==b)return b-a}
+  return(QUAL&&!IS_JURY&&HAS_F)?((y.score??-1e18)-(x.score??-1e18)):((y.r?y.r.bytes:-1)-(x.r?x.r.bytes:-1))});
  const hiddenN=allRows.filter(x=>x.r&&x.r.bytes<MIN_BYTES).length;
  const rows=allRows.filter(x=>!(x.r&&x.r.bytes<MIN_BYTES)&&!(IS_JURY&&QUAL&&x.r&&!x.mine));
  ROWS_CUR=rows;
  const maxB=Math.max(1,...rows.map(r=>r.r?r.r.bytes:0));
  const th=(lbl,tip)=>'<th title="'+esc(tip||'')+'">'+esc(lbl)+'</th>';
+ const thS=(lbl,tip,k)=>'<th title="'+esc(tip||'')+'" style="cursor:pointer;user-select:none;white-space:nowrap" onclick="setSort(\\''+k+'\\')">'+esc(lbl)+(SORT_KEY===k?' ▼':' ⇅')+'</th>';
  let h='<table><thead><tr><th>#</th>'+th(T.th_u,T.tip_th_u);
  if(IS_JURY){
-  h+=th(T.th_b,T.tip_th_b)+th(T.th_created_n,T.tip_th_created_n)+th(T.th_expanded_n,T.tip_th_expanded_n)+th(T.th_avg,T.tip_th_avg)+th(T.th_progress,T.tip_th_progress);
+  h+=th(T.th_b,T.tip_th_b)+th(T.th_created_n,T.tip_th_created_n)+th(T.th_expanded_n,T.tip_th_expanded_n)+th(T.th_avg,T.tip_th_avg)+thS(T.th_avg_c,T.tip_th_avg_c,'qc')+thS(T.th_avg_e,T.tip_th_avg_e,'qe')+th(T.th_progress,T.tip_th_progress);
  }else{
   if(QUANT){h+=th(T.th_b,T.tip_th_b);projects.forEach(p=>h+='<th>'+esc(p.label)+'</th>');h+=th(T.th_e,T.tip_th_e)+th(T.th_split,T.tip_th_split)}
-  if(QUAL){h+=th(T.th_avg,T.tip_th_avg);if(HAS_F)h+=th(T.th_score,T.tip_th_score);h+=th(T.th_progress,T.tip_th_progress)}
+  if(QUAL){h+=th(T.th_avg,T.tip_th_avg)+thS(T.th_avg_c,T.tip_th_avg_c,'qc')+thS(T.th_avg_e,T.tip_th_avg_e,'qe');if(HAS_F)h+=th(T.th_score,T.tip_th_score);h+=th(T.th_progress,T.tip_th_progress)}
  }
  h+='<th></th></tr></thead><tbody>';
  if(!rows.length)h+='<tr><td colspan="99" class="hint" style="padding:20px">'+T.no_parts+'</td></tr>';
@@ -1283,6 +1429,8 @@ function render(){
    h+='<td class="mono" style="font-size:12px;color:#1d7a4f">'+(row.r?fmtN(row.sp.created):'—')+'</td>';
    h+='<td class="mono" style="font-size:12px">'+(row.r?fmtN(row.sp.expanded):'—')+'</td>';
    h+='<td class="mono">'+(row.n?row.quality.toFixed(2):'—')+'<div class="hint">'+row.n+' '+T.scores_n+'</div></td>';
+   h+='<td class="mono">'+(row.kq.qc!=null?row.kq.qc.toFixed(2):'—')+'<div class="hint">'+row.kq.qcn+' '+T.th_created_n.toLowerCase()+'</div></td>';
+   h+='<td class="mono">'+(row.kq.qe!=null?row.kq.qe.toFixed(2):'—')+'<div class="hint">'+row.kq.qen+' '+T.th_expanded_n.toLowerCase()+'</div></td>';
    h+='<td class="mono" style="font-size:12px">'+(row.total?row.done+' / '+row.total:'—')+'</td>';
   }
   if(QUANT&&!IS_JURY){
@@ -1296,6 +1444,8 @@ function render(){
   }
   if(QUAL&&!IS_JURY){
    h+='<td class="mono">'+(row.n?row.quality.toFixed(2):'—')+'<div class="hint">'+row.n+' '+T.scores_n+'</div></td>';
+   h+='<td class="mono">'+(row.kq.qc!=null?row.kq.qc.toFixed(2):'—')+'<div class="hint">'+row.kq.qcn+' '+T.th_created_n.toLowerCase()+'</div></td>';
+   h+='<td class="mono">'+(row.kq.qe!=null?row.kq.qe.toFixed(2):'—')+'<div class="hint">'+row.kq.qen+' '+T.th_expanded_n.toLowerCase()+'</div></td>';
    if(HAS_F)h+='<td><span class="mono" style="font-weight:700;color:var(--gold);font-size:15px">'+(row.score!=null?row.score.toFixed(2):'—')+'</span></td>';
    h+='<td class="mono" style="font-size:12px">'+(row.total?row.done+' / '+row.total:'—')
     +(row.mine&&!IS_JURY?'<div class="hint">'+T.my_assigned+': '+row.mineDone+' / '+row.mine+'</div>':'')+'</td>';
@@ -1387,12 +1537,19 @@ function render(){
   }
  });
  h+='</tbody></table>';
- if(hiddenN)h+='<div class="hint" style="padding:8px 12px">'+T.hidden_min+' ('+fmtN(MIN_BYTES)+(LANG==='uk'?' Б':' B')+'): '+hiddenN+'</div>';
+ if(hiddenN&&MIN_BYTES>0)h+='<div class="hint" style="padding:8px 12px">'+T.hidden_min+' ('+fmtN(MIN_BYTES)+BU+'): '+hiddenN+'</div>';
+ if(MIN_EXP>0||MIN_NEW>0){
+  let hiddenE=0;
+  rows.forEach(rw=>{if(rw.r)Object.values(rw.r.perProject||{}).forEach(pp=>(pp.list||[]).forEach(it=>{if((it.bytes||0)<(it.created?MIN_NEW:MIN_EXP))hiddenE++}))});
+  h+='<div class="hint" style="padding:8px 12px">'+T.hidden_exp+' ('+fmtN(MIN_NEW)+' / '+fmtN(MIN_EXP)+BU+'): '+hiddenE+'</div>';
+ }
  el.innerHTML=h;
+ const tb=document.getElementById('topbox');
+ if(tb){if(QUAL&&!IS_JURY){tb.style.display='block';tb.innerHTML=topHtml(rows)}else{tb.style.display='none';tb.innerHTML=''}}
  const nc=el.querySelector('.carousel');if(nc&&keepScroll)nc.scrollLeft=keepScroll;
  el.querySelectorAll('.art-filt-btn').forEach(b=>{b.onclick=function(){setArtFilter(this.dataset.f)}});
  el.querySelectorAll('.rev-filt-btn').forEach(b=>{b.onclick=function(){setRevFilter(this.dataset.f)}});
- document.getElementById('csvlink').href='/export/'+CID+'.csv?quant='+(QUANT?1:0)+'&qual='+(QUAL?1:0);
+ document.getElementById('csvlink').href='/export/'+CID+'.csv?quant='+(QUANT?1:0)+'&qual='+(QUAL?1:0)+(SORT_KEY?'&sort='+SORT_KEY:'');
  renderLoad();
  renderModal();
 }
@@ -1446,6 +1603,42 @@ ADMIN_TPL = """
     {% for k in ['all','created','expanded'] %}<option value="{{k}}" {{'selected' if (edit_contest.review_scope if edit_contest else 'all')==k else ''}}>{{t['scope_'~k]}}</option>{% endfor %}
    </select>
    <div class="hint">{{t.scope_h}}</div>
+   <label class="lbl" title="{{t.tip_min_bytes}}">{{t.min_bytes}}</label>
+   <input type="number" name="min_bytes" min="0" step="1" style="max-width:160px" title="{{t.tip_min_bytes}}" value="{{(edit_contest.min_bytes or 0) if edit_contest else 0}}">
+   <div class="hint">{{t.min_bytes_h}}</div>
+   <label class="lbl" title="{{t.tip_min_expand}}">{{t.min_expand}}</label>
+   <input type="number" name="min_expand" min="0" step="1" style="max-width:160px" title="{{t.tip_min_expand}}" value="{{(edit_contest.min_expand or 0) if edit_contest else 0}}">
+   <div class="hint">{{t.min_expand_h}}</div>
+   <label class="lbl" title="{{t.tip_min_created}}">{{t.min_created}}</label>
+   <input type="number" name="min_created" min="0" step="1" style="max-width:160px" title="{{t.tip_min_created}}" value="{{(edit_contest.min_created or 0) if edit_contest else 0}}">
+   <div class="hint">{{t.min_created_h}}</div>
+   {% if edit_contest %}
+   <div id="minExpWarn" class="hint" style="display:none;margin-top:6px;padding:8px 10px;border-radius:8px"></div>
+   <script>
+   (function(){
+    var CID_E={{edit_contest.id|tojson}},MSG={warn:{{t.me_warn|tojson}},ok:{{t.me_ok|tojson}},confirm:{{t.me_confirm|tojson}},more:{{t.me_more|tojson}}};
+    var inpB=document.querySelector('input[name=min_bytes]'),inp=document.querySelector('input[name=min_expand]'),inpN=document.querySelector('input[name=min_created]'),box=document.getElementById('minExpWarn'),sel=document.querySelector('select[name=review_scope]');
+    var form=inp.form,cnt=0,tm=null,seq=0;
+    function esc(x){return String(x).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
+    function check(){
+     var my=++seq,v=inp.value||'0';
+     fetch('/api/min_expand_preview/'+encodeURIComponent(CID_E)+'?value='+encodeURIComponent(v)+'&value_new='+encodeURIComponent(inpN.value||'0')+'&value_bytes='+encodeURIComponent(inpB.value||'0')+'&scope='+encodeURIComponent(sel?sel.value:'')).then(function(r){return r.json()}).then(function(d){
+      if(my!==seq||d.error)return;
+      cnt=d.count;box.style.display='block';
+      if(!d.count){box.style.background='#e6f4ea';box.textContent=MSG.ok;return}
+      box.style.background='#fdecea';
+      var h='<b>'+esc(MSG.warn.replace('{n}',d.count))+'</b><ul style="margin:6px 0 0 18px;padding:0">';
+      d.items.forEach(function(x){h+='<li>'+esc(x.participant)+' — '+esc(x.article)+' (+'+x.bytes+', '+x.scores+')</li>'});
+      if(d.count>d.items.length)h+='<li>'+esc(MSG.more)+' '+(d.count-d.items.length)+'</li>';
+      box.innerHTML=h+'</ul>';
+     });
+    }
+    function later(){clearTimeout(tm);tm=setTimeout(check,300)}
+    inp.addEventListener('input',later);inpN.addEventListener('input',later);inpB.addEventListener('input',later);if(sel)sel.addEventListener('change',later);
+    form.addEventListener('submit',function(e){if(cnt>0&&!confirm(MSG.confirm.replace('{n}',cnt)))e.preventDefault()});
+   })();
+   </script>
+   {% endif %}
    <label class="lbl" title="{{t.tip_cjury}}">{{t.contest_jury}}</label>
    <div style="display:flex;flex-wrap:wrap;gap:6px 14px;border:1px solid var(--line);border-radius:8px;padding:8px 10px;background:#fff">
     {% for u in users if u.role=='jury' %}
@@ -1575,7 +1768,8 @@ ANALYSIS_TPL = """
  <div id="loadtbl" style="margin-top:8px"></div>
 </div>
 <div class="card" style="padding:0;overflow-x:auto"><div id="tbl"></div></div>
-<div class="hint">{% if me.role=='admin' %}{{t.f_of}} <span class="chip">{{contest.formula if contest.formula else '—'}}</span> · {% endif %}{{t.scope_label}} <span class="chip">{{t['scope_'~(contest.review_scope or 'all')]}}</span></div>
+<div id="topbox" class="card" style="display:none"></div>
+<div class="hint">{% if me.role=='admin' %}{{t.f_of}} <span class="chip">{{contest.formula if contest.formula else '—'}}</span> · {% endif %}{{t.scope_label}} <span class="chip">{{t['scope_'~(contest.review_scope or 'all')]}}</span>{% if contest.min_bytes %} · {{t.min_bytes_chip}} <span class="chip">{{contest.min_bytes}} {{t.bytes_unit}}</span>{% endif %}{% if contest.min_created %} · {{t.min_created_chip}} <span class="chip">{{contest.min_created}} {{t.bytes_unit}}</span>{% endif %}{% if contest.min_expand %} · {{t.min_expand_chip}} <span class="chip">{{contest.min_expand}} {{t.bytes_unit}}</span>{% endif %}</div>
 {% endif %}
 {% if me.role=='jury' %}
 <div class="card" style="max-width:400px;margin-top:14px">
@@ -1665,16 +1859,42 @@ def get_legacy(cid):
             {"jury": r["jury_name"], "text": r["text"], "date": r["date"]})
     return out
 
-def in_scope(it, scope):
-    """Чи входить стаття (запис зі списку внеску) до обраного адміном обсягу перевірки."""
+def in_scope(it, scope, min_exp=0, min_new=0):
+    """Чи входить стаття (запис зі списку внеску) до обраного адміном обсягу перевірки.
+    Доповнення, у яких додано менше min_exp байтів, і нові статті менші за min_new відсіюються."""
+    if (it.get("bytes") or 0) < (min_new if it.get("created") else min_exp):
+        return False
     return scope == "all" or (scope == "created") == bool(it.get("created"))
 
-def scope_titles(r, scope):
+def scope_titles(r, scope, min_exp=0, min_new=0):
     """Назви статей учасника в обсязі перевірки (None = без обмежень)."""
-    if scope == "all" or not r:
+    if (scope == "all" and not min_exp and not min_new) or not r:
         return None
     return {it.get("title") for pp in (r.get("perProject") or {}).values()
-            for it in (pp.get("list") or []) if in_scope(it, scope)}
+            for it in (pp.get("list") or []) if in_scope(it, scope, min_exp, min_new)}
+
+def parse_min_expand(v):
+    try:
+        return max(0, int(float(str(v).replace(",", ".").strip() or 0)))
+    except (TypeError, ValueError):
+        return 0
+
+def kind_quality(participant_assess, r, scope, min_exp, min_new=0):
+    """Середній бал журі окремо для нових і доповнених статей учасника (у межах обсягу перевірки).
+    Повертає {"created": (середнє|None, кількість оцінених статей), "expanded": (...)}."""
+    kinds = {"created": [], "expanded": []}
+    seen = set()
+    for pp in ((r or {}).get("perProject") or {}).values():
+        for it in (pp.get("list") or []):
+            t = it.get("title")
+            if not t or t in seen or not in_scope(it, scope, min_exp, min_new):
+                continue
+            seen.add(t)
+            vals = [v for v in ((participant_assess or {}).get(t, {}).get("scores") or {}).values()
+                    if v is not None]
+            if vals:
+                kinds["created" if it.get("created") else "expanded"].append(sum(vals) / len(vals))
+    return {k: ((sum(v) / len(v)) if v else None, len(v)) for k, v in kinds.items()}
 
 def article_quality(participant_assess, legacy=None, allowed=None):
     """Якість учасника = середнє арифметичне середніх оцінок по кожній статті
@@ -1797,7 +2017,7 @@ def index():
         cjson=json.dumps({k: v for k, v in contest.items() if me.role == "admin" or k != "jury"},
                          ensure_ascii=False) if contest else "null",
         rjson=json.dumps(results, ensure_ascii=False) if results else "null",
-        ajson=json.dumps(assess, ensure_ascii=False), min_bytes=MIN_BYTES,
+        ajson=json.dumps(assess, ensure_ascii=False),
         juryjson=json.dumps([{"login": u.login, "name": u.name, "role": u.role,
                               "inc": u.login in ((contest.jury or []) if contest else [])} for u in users
                              if me.role == "admin" or u.login == me.login],
@@ -1816,14 +2036,19 @@ def contest_save():
         formula = ""  # некоректну формулу не зберігаємо; порожня = формула не використовується
     cid = f.get("id") or "c" + hex(int(time.time() * 1000))[2:]
     scope = f.get("review_scope") if f.get("review_scope") in ("all", "created", "expanded") else "all"
-    db().execute("""INSERT INTO contests VALUES(?,?,?,?,?,?,?,?,?,?)
+    min_exp = parse_min_expand(f.get("min_expand"))
+    min_new = parse_min_expand(f.get("min_created"))
+    min_b = parse_min_expand(f.get("min_bytes"))
+    db().execute("""INSERT INTO contests(id,name,template,projects,participants,formula,start,end,
+        tpl_talk_only,review_scope,min_expand,min_created,min_bytes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
         ON CONFLICT(id) DO UPDATE SET name=excluded.name,template=excluded.template,
         projects=excluded.projects,participants=excluded.participants,
         formula=excluded.formula,start=excluded.start,end=excluded.end,
-        tpl_talk_only=excluded.tpl_talk_only,review_scope=excluded.review_scope""",
+        tpl_talk_only=excluded.tpl_talk_only,review_scope=excluded.review_scope,
+        min_expand=excluded.min_expand,min_created=excluded.min_created,min_bytes=excluded.min_bytes""",
         (cid, f.get("name", "").strip(), f.get("template", "").strip(),
          json.dumps(projects), json.dumps(participants), formula,
-         f.get("start", ""), f.get("end", ""), 1 if f.get("tpl_talk_only") else 0, scope))
+         f.get("start", ""), f.get("end", ""), 1 if f.get("tpl_talk_only") else 0, scope, min_exp, min_new, min_b))
     # журі конкурсу: оновлюємо лише склад; оцінки, перевірки та призначення не чіпаємо
     valid = {r["login"] for r in db().execute("SELECT login FROM users WHERE role='jury'")}
     chosen = [j for j in f.getlist("jury") if j in valid]
@@ -1979,16 +2204,19 @@ def api_distribute(cid):
     results = json.loads(rr["data"]) if rr else {}
     items, seen = [], set()
     legacy = get_legacy(cid)
-    srow = con.execute("SELECT review_scope FROM contests WHERE id=?", (cid,)).fetchone()
+    srow = con.execute("SELECT review_scope, min_expand, min_created, min_bytes FROM contests WHERE id=?", (cid,)).fetchone()
+    min_b = (srow["min_bytes"] if srow else 0) or 0
     scope = (srow["review_scope"] if srow else None) or "all"
+    min_exp = (srow["min_expand"] if srow else 0) or 0
+    min_new = (srow["min_created"] if srow else 0) or 0
     for user in sorted(results):
         if user in legacy and (legacy[user]["scores"] or legacy[user]["comments"]):
             continue  # внесок уже перевірено в попередній версії
-        if ((results[user] or {}).get("bytes") or 0) < MIN_BYTES:
+        if ((results[user] or {}).get("bytes") or 0) < min_b:
             continue  # внесок нижчий за мінімальний поріг
         for pp in ((results[user] or {}).get("perProject") or {}).values():
             for it in (pp.get("list") or []):
-                if not in_scope(it, scope):
+                if not in_scope(it, scope, min_exp, min_new):
                     continue
                 k = (user, it.get("title", ""))
                 if k[1] and k not in seen:
@@ -2097,6 +2325,48 @@ def api_reassign(cid):
     con.commit()
     return jsonify(ok=True, moved=moved, merged=merged, kept=kept)
 
+@app.route("/api/min_expand_preview/<cid>")
+@login_required
+def api_min_expand_preview(cid):
+    """Які вже оцінені статті (доповнення й нові) випадуть з перевірки, якщо задати нові мінімуми (лише адмін).
+    Порівнюємо з тим, що діє зараз (збережені обсяг і поріг)."""
+    if session["user"]["role"] != "admin":
+        return jsonify(error="forbidden"), 403
+    con = db()
+    crow = con.execute("SELECT review_scope, min_expand, min_created, min_bytes FROM contests WHERE id=?", (cid,)).fetchone()
+    if not crow:
+        return jsonify(error="not_found"), 404
+    old_scope = crow["review_scope"] or "all"
+    old_min = crow["min_expand"] or 0
+    old_new = crow["min_created"] or 0
+    old_b = crow["min_bytes"] or 0
+    new_b = parse_min_expand(request.args.get("value_bytes")) if "value_bytes" in request.args else old_b
+    new_min = parse_min_expand(request.args.get("value"))
+    new_new = parse_min_expand(request.args.get("value_new")) if "value_new" in request.args else old_new
+    new_scope = request.args.get("scope") if request.args.get("scope") in ("all", "created", "expanded") else old_scope
+    rr = con.execute("SELECT data FROM results WHERE contest_id=?", (cid,)).fetchone()
+    results = json.loads(rr["data"]) if rr else {}
+    scored = {}
+    for r in con.execute("SELECT participant, article, COUNT(*) n FROM article_scores "
+                         "WHERE contest_id=? AND value IS NOT NULL GROUP BY participant, article", (cid,)):
+        scored[(r["participant"], r["article"])] = r["n"]
+    items, seen = [], set()
+    for user, res in (results or {}).items():
+        ub = (res or {}).get("bytes") or 0
+        for pp in ((res or {}).get("perProject") or {}).values():
+            for it in (pp.get("list") or []):
+                k = (user, it.get("title", ""))
+                if k in seen or k not in scored:
+                    continue
+                old_ok = ub >= old_b and in_scope(it, old_scope, old_min, old_new)
+                new_ok = ub >= new_b and in_scope(it, new_scope, new_min, new_new)
+                if old_ok and not new_ok:
+                    seen.add(k)
+                    items.append({"participant": user, "article": k[1],
+                                  "bytes": it.get("bytes", 0), "scores": scored[k]})
+    items.sort(key=lambda x: (x["participant"], x["article"]))
+    return jsonify(count=len(items), items=items[:15])
+
 @app.route("/api/state/<cid>")
 @login_required
 def api_state(cid):
@@ -2123,6 +2393,10 @@ def export_csv(cid):
     assess = get_assess(cid)
     legacy = get_legacy(cid)
     scope = contest.get("review_scope") or "all"
+    min_exp = contest.get("min_expand") or 0
+    min_new = contest.get("min_created") or 0
+    min_b = contest.get("min_bytes") or 0
+    sort_key = request.args.get("sort") if request.args.get("sort") in ("qc", "qe") else None
     show_score = bool((contest.get("formula") or "").strip()) and session["user"]["role"] == "admin"
     hosts = []
     for p in contest["projects"]:
@@ -2133,11 +2407,12 @@ def export_csv(cid):
     rows = []
     for user in contest["participants"]:
         r = results.get(user)
-        if r and (r.get("bytes") or 0) < MIN_BYTES:
+        if r and (r.get("bytes") or 0) < min_b:
             continue  # нижче мінімального порогу
         a = assess.get(user, {})
-        allowed = scope_titles(r, scope)
+        allowed = scope_titles(r, scope, min_exp, min_new)
         quality, n = article_quality(a, legacy.get(user), allowed)
+        kq = kind_quality(a, r, scope, min_exp, min_new)
         sp = split_stats(r)
         score = eval_formula(contest["formula"],
                              {"bytes": r["bytes"], "edits": r["edits"],
@@ -2150,9 +2425,13 @@ def export_csv(cid):
                 continue
             for c in ad.get("comments", []):
                 all_comments.append(f"[{art}] {c['jury']} ({c['date']}): {c['text']}")
-        rows.append((user, r, all_comments, quality, n, score))
-    rows.sort(key=lambda x: -(x[5] if (qual and x[5] is not None)
-                              else (x[1]["bytes"] if x[1] else -1)))
+        rows.append((user, r, all_comments, quality, n, score, kq))
+    if sort_key and qual:
+        kind = "created" if sort_key == "qc" else "expanded"
+        rows.sort(key=lambda x: -(x[6][kind][0] if x[6][kind][0] is not None else -1e18))
+    else:
+        rows.sort(key=lambda x: -(x[5] if (qual and x[5] is not None)
+                                  else (x[1]["bytes"] if x[1] else -1)))
 
     buf = io.StringIO()
     w = csv.writer(buf, delimiter=",")
@@ -2160,9 +2439,9 @@ def export_csv(cid):
     if quant:
         head += [t["th_b"]] + hosts + [t["th_e"], t["th_split"], t["th_split_b"]]
     if qual:
-        head += [t["th_avg"]] + ([t["th_score"]] if show_score else []) + [t["comments"]]
+        head += [t["th_avg"], t["th_avg_c"], t["th_avg_e"]] + ([t["th_score"]] if show_score else []) + [t["comments"]]
     w.writerow(head)
-    for i, (user, r, all_comments, quality, n, score) in enumerate(rows, 1):
+    for i, (user, r, all_comments, quality, n, score, kq) in enumerate(rows, 1):
         line = [i, user]
         if quant:
             line.append(r["bytes"] if r else "")
@@ -2175,6 +2454,8 @@ def export_csv(cid):
             line.append(f"{sp['bc']} / {sp['be']}" if sp else "")
         if qual:
             line.append(round(quality, 2) if n else "")
+            line.append(round(kq["created"][0], 2) if kq["created"][0] is not None else "")
+            line.append(round(kq["expanded"][0], 2) if kq["expanded"][0] is not None else "")
             if show_score:
                 line.append(round(score, 2) if score is not None else "")
             line.append(" | ".join(all_comments))
diff --git a/test_app.py b/test_app.py
index 5ecaee4..b3b9c71 100644
--- a/test_app.py
+++ b/test_app.py
@@ -451,3 +451,150 @@ def teardown_module(_):
         os.unlink(_db_path)
     except OSError:
         pass
+
+
+# ─────────────────────────────────────────────────────────────
+# Мінімум байтів для доповнень + рейтинг статей за балами журі
+# ─────────────────────────────────────────────────────────────
+class TestMinExpandAndRanking:
+    LIST = [
+        {"title": "Нова мала", "bytes": 300, "created": True},
+        {"title": "Доп велика", "bytes": 5000, "created": False},
+        {"title": "Доп мала", "bytes": 200, "created": False},
+    ]
+
+    def test_in_scope_drops_small_expansions_only(self):
+        small_exp, big_exp, small_new = self.LIST[2], self.LIST[1], self.LIST[0]
+        assert wr.in_scope(small_exp, "all", 0)
+        assert not wr.in_scope(small_exp, "all", 500)
+        assert wr.in_scope(big_exp, "all", 500)
+        assert wr.in_scope(small_new, "all", 500)        # нові не відсіюються
+        assert not wr.in_scope(small_exp, "expanded", 500)
+
+    def test_scope_titles_with_threshold(self):
+        r = {"perProject": {"h": {"list": self.LIST}}}
+        assert wr.scope_titles(r, "all", 0) is None
+        assert wr.scope_titles(r, "all", 500) == {"Нова мала", "Доп велика"}
+
+    def test_parse_min_expand(self):
+        assert wr.parse_min_expand("1500") == 1500
+        assert wr.parse_min_expand("-5") == 0
+        assert wr.parse_min_expand("abc") == 0
+        assert wr.parse_min_expand("") == 0
+
+    def test_kind_quality_separates_created_and_expanded(self):
+        r = {"perProject": {"h": {"list": self.LIST}}}
+        a = {"Нова мала": {"scores": {"j1": 8, "j2": 6}},
+             "Доп велика": {"scores": {"j1": 4}},
+             "Доп мала": {"scores": {"j1": 10}}}
+        kq = wr.kind_quality(a, r, "all", 0)
+        assert kq["created"] == (7.0, 1)
+        assert kq["expanded"] == (7.0, 2)               # (4 + 10) / 2
+        kq = wr.kind_quality(a, r, "all", 500)           # «Доп мала» відсіяна
+        assert kq["expanded"] == (4.0, 1)
+
+    def test_save_min_expand_and_distribute_skips_small(self, admin):
+        make_jury(admin, "jx", "jurypass", "Журі Х")
+        admin.post("/admin/contest/save", data={
+            "id": "cme", "name": "Мін доповнення", "participants": "U1",
+            "projects": "https://uk.wikivoyage.org/wiki/X", "jury": ["jx"],
+            "min_expand": "500", "review_scope": "all"})
+        with wr.app.app_context():
+            row = wr.db().execute("SELECT min_expand FROM contests WHERE id='cme'").fetchone()
+            assert row["min_expand"] == 500
+        admin.post("/api/results/cme", json={"U1": {
+            "bytes": 5500, "edits": 3, "articles": 3,
+            "perProject": {"uk.wikivoyage.org": {"bytes": 5500, "list": self.LIST}}}})
+        resp = admin.post("/api/distribute/cme", json={"per_article": 1})
+        assert resp.get_json()["items"] == 2
+        with wr.app.app_context():
+            arts = {r["article"] for r in wr.db().execute(
+                "SELECT article FROM article_assignments WHERE contest_id='cme'")}
+        assert arts == {"Нова мала", "Доп велика"}
+
+    def test_export_has_split_score_columns_and_sort(self, admin):
+        admin.post("/admin/contest/save", data={
+            "id": "cex", "name": "Експорт", "participants": "A\nB",
+            "projects": "https://uk.wikivoyage.org/wiki/X"})
+        mk = lambda b: {"bytes": b, "edits": 1, "articles": 2, "perProject": {"uk.wikivoyage.org": {
+            "bytes": b, "list": [{"title": f"n{b}", "bytes": b, "created": True},
+                                 {"title": f"e{b}", "bytes": b, "created": False}]}}}
+        admin.post("/api/results/cex", json={"A": mk(2000), "B": mk(3000)})
+        with wr.app.app_context():
+            con = wr.db()
+            for p, art, v in (("A", "n2000", 9), ("A", "e2000", 3), ("B", "n3000", 2), ("B", "e3000", 8)):
+                con.execute("INSERT INTO article_scores VALUES('cex',?,?,?,?)", (p, art, "j", v))
+            con.commit()
+        body = admin.get("/export/cex.csv?quant=1&qual=1&sort=qc").data.decode("utf-8-sig")
+        lines = body.strip().splitlines()
+        assert "Бал: нові" in lines[0] and "Бал: доповнені" in lines[0]
+        assert lines[1].split(",")[1] == "A"             # A має вищий бал за нові
+        body = admin.get("/export/cex.csv?quant=1&qual=1&sort=qe").data.decode("utf-8-sig")
+        assert body.strip().splitlines()[1].split(",")[1] == "B"
+
+
+class TestMinExpandPreview:
+    def test_preview_lists_scored_expansions_that_would_drop(self, admin):
+        admin.post("/admin/contest/save", data={
+            "id": "cpv", "name": "Прев'ю", "participants": "P",
+            "projects": "https://uk.wikivoyage.org/wiki/X"})
+        lst = [{"title": "Нова", "bytes": 50, "created": True},
+               {"title": "Мала доп", "bytes": 200, "created": False},
+               {"title": "Велика доп", "bytes": 5000, "created": False},
+               {"title": "Мала без оцінки", "bytes": 100, "created": False}]
+        admin.post("/api/results/cpv", json={"P": {"bytes": 5350, "edits": 1, "articles": 4,
+                   "perProject": {"h": {"bytes": 5350, "list": lst}}}})
+        for a in ("Нова", "Мала доп", "Велика доп"):
+            admin.post("/api/score", json={"contest": "cpv", "participant": "P", "article": a, "value": 7})
+        d = admin.get("/api/min_expand_preview/cpv?value=1000").get_json()
+        assert d["count"] == 1 and d["items"][0]["article"] == "Мала доп"   # нова й без оцінки не рахуються
+        assert admin.get("/api/min_expand_preview/cpv?value=0").get_json()["count"] == 0
+
+    def test_preview_admin_only(self, admin):
+        make_jury(admin, "jp", "jurypass", "Журі П")
+        login_as(admin, "jp", "jurypass")
+        assert admin.get("/api/min_expand_preview/cpv?value=10").status_code == 403
+
+
+class TestMinCreated:
+    def test_in_scope_min_new(self):
+        new_small = {"title": "n", "bytes": 3000, "created": True}
+        new_big = {"title": "N", "bytes": 4000, "created": True}
+        exp_small = {"title": "e", "bytes": 3000, "created": False}
+        assert not wr.in_scope(new_small, "all", 0, 3500)
+        assert wr.in_scope(new_big, "all", 0, 3500)
+        assert wr.in_scope(exp_small, "all", 0, 3500)          # поріг нових не діє на доповнення
+        assert not wr.in_scope(exp_small, "all", 3500, 0)
+        assert wr.scope_titles({"perProject": {"h": {"list": [new_small, new_big]}}}, "all", 0, 3500) == {"N"}
+
+    def test_save_and_distribute_with_min_created(self, admin):
+        make_jury(admin, "jm", "jurypass", "Журі М")
+        admin.post("/admin/contest/save", data={
+            "id": "cmc", "name": "Мін нова", "participants": "U", "jury": ["jm"],
+            "projects": "https://uk.wikivoyage.org/wiki/X", "min_created": "3500", "min_expand": "3500"})
+        lst = [{"title": "Нова мала", "bytes": 3000, "created": True},
+               {"title": "Нова ок", "bytes": 4000, "created": True},
+               {"title": "Доп мала", "bytes": 3000, "created": False},
+               {"title": "Доп ок", "bytes": 3600, "created": False}]
+        admin.post("/api/results/cmc", json={"U": {"bytes": 13600, "edits": 4, "articles": 4,
+                   "perProject": {"h": {"bytes": 13600, "list": lst}}}})
+        assert admin.post("/api/distribute/cmc", json={}).get_json()["items"] == 2
+        admin.post("/api/score", json={"contest": "cmc", "participant": "U", "article": "Нова ок", "value": 5})
+        d = admin.get("/api/min_expand_preview/cmc?value=3500&value_new=4500").get_json()
+        assert d["count"] == 1 and d["items"][0]["article"] == "Нова ок"   # 4000 < 4500
+        assert admin.get("/api/min_expand_preview/cmc?value=3500&value_new=3500").get_json()["count"] == 0
+        d = admin.get("/api/min_expand_preview/cmc?value=3500&value_new=3500&value_bytes=99999").get_json()
+        assert d["count"] == 1    # поріг внеску учасника теж відсіює
+
+
+class TestMinBytesConfigurable:
+    def test_default_is_off_and_configurable(self, admin):
+        make_jury(admin, "jb", "jurypass", "Журі Б")
+        for cid, mb in (("cb0", ""), ("cb1", "5000")):
+            admin.post("/admin/contest/save", data={
+                "id": cid, "name": cid, "participants": "S", "jury": ["jb"],
+                "projects": "https://uk.wikivoyage.org/wiki/X", "min_bytes": mb})
+            admin.post(f"/api/results/{cid}", json={"S": {"bytes": 2000, "edits": 1, "articles": 1,
+                       "perProject": {"h": {"bytes": 2000, "list": [{"title": "T", "bytes": 2000, "created": True}]}}}})
+        assert admin.post("/api/distribute/cb0", json={}).get_json().get("items") == 1   # без порога
+        assert admin.post("/api/distribute/cb1", json={}).status_code == 400            # 2000 < 5000
