"""
browser.py — Automação do fluxo de importação de NF-e
Fluxo: Integração > EDI Importação > Fretes > Nova Importação > XML NF-e
       > Cliente > Filial > Upload XML(s) > Salvar
"""

import base64
import hashlib
import logging
import os
from datetime import datetime

from session import get_authenticated_page, limpar_sessao

logger = logging.getLogger(__name__)

BASE_URL    = "https://mandalog.eslcloud.com.br"
BATCHES_URL = f"{BASE_URL}/edi/import/batches"


async def importar_xml(
    browser,
    customer_id: str,
    customer_name: str,
    corporation_name: str,
    xml_files: list,
) -> dict:
    """
    Executa o fluxo completo de importação de 1 ou mais XMLs NF-e.
    Tenta até 2 vezes em caso de sessão expirada.
    """
    page = None
    context = None
    tentativa = 0

    while tentativa < 2:
        tentativa += 1
        try:
            caminhos_debug = _salvar_xmls_recebidos(xml_files)
            logger.info("XML(s) recebidos salvos para conferencia em: %s", ", ".join(caminhos_debug))
            page, context = await get_authenticated_page(browser)
            resultado = await _executar_importacao(
                page, customer_id, customer_name, corporation_name, xml_files
            )
            return resultado

        except Exception as e:
            mensagem = str(e)
            logger.error("Erro na tentativa %d: %s", tentativa, mensagem)

            if tentativa == 1 and _e_erro_de_sessao(mensagem, page):
                logger.warning("Sessão inválida — limpando e retentando...")
                await limpar_sessao()
                continue

            screenshot_b64 = await _screenshot_seguro(page)
            return {
                "sucesso":    False,
                "erro":       mensagem,
                "passo":      "inicialização / sessão",
                "screenshot": screenshot_b64,
            }


    return {"sucesso": False, "erro": "Todas as tentativas falharam.", "passo": "inicialização", "screenshot": None}


# ── Fluxo principal ────────────────────────────────────────────────────────────

async def _executar_importacao(
    page,
    customer_id: str,
    customer_name: str,
    corporation_name: str,
    xml_files: list,
) -> dict:

    # ── PASSO 1: Navega direto para a tela de Fretes ─────────────────────────
    passo = "Passo 1 — Navegar para tela de Fretes"
    try:
        logger.info(passo)
        await page.goto(BATCHES_URL)
        await page.wait_for_load_state("networkidle")
        await page.wait_for_selector('a.btn.btn-primary.floating[data-toggle="dropdown"]', timeout=15000)
        logger.info("URL atual: %s", page.url)
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 2: Abre dropdown "Nova Importação" ──────────────────────────────
    passo = "Passo 2 — Abrir dropdown Nova Importação"
    try:
        logger.info(passo)
        gatilho_dropdown = page.locator('a.btn.btn-primary.floating[data-toggle="dropdown"]').first
        await gatilho_dropdown.click()
        await page.wait_for_function(
            """() => {
                const menus = Array.from(document.querySelectorAll('.dropdown-menu'));
                return menus.some((menu) => {
                    const style = window.getComputedStyle(menu);
                    return style.display !== 'none' && style.visibility !== 'hidden' && menu.offsetParent !== null;
                });
            }""",
            timeout=5000,
        )
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 3: Clica em "XML - NF-e" ───────────────────────────────────────
    passo = "Passo 3 — Clicar em XML - NF-e"
    try:
        logger.info(passo)
        clicou = await page.evaluate(
            """() => {
                const normalizar = (valor) => String(valor || '').replace(/\\s+/g, ' ').trim().toUpperCase();
                const menus = Array.from(document.querySelectorAll('.dropdown-menu')).filter((menu) => {
                    const style = window.getComputedStyle(menu);
                    return style.display !== 'none' && style.visibility !== 'hidden' && menu.offsetParent !== null;
                });

                for (const menu of menus) {
                    const link = Array.from(menu.querySelectorAll('a')).find((el) => {
                        const href = el.getAttribute('href') || '';
                        const texto = normalizar(el.textContent);
                        return href === '/edi/import/batches/new?file_format=xml' && texto === 'XML - NF-E';
                    });
                    if (link) {
                        link.click();
                        return true;
                    }
                }
                return false;
            }""",
        )
        if not clicou:
            raise RuntimeError("Link exato de XML - NF-e nao encontrado no dropdown aberto.")
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 4: Aguarda modal carregar ───────────────────────────────────────
    passo = "Passo 4 — Aguardar modal carregar"
    try:
        logger.info(passo)
        await page.wait_for_selector('#upload-modal, #edi_import_batch_upload', timeout=15000)
        await page.wait_for_selector('#edi_import_batch_upload[action="/edi/import/batches"], form#edi_import_batch_upload', timeout=15000)
        await page.wait_for_selector('#edi_import_batch_customer_id', timeout=15000)
        await _validar_modal_importacao(page)
        await page.wait_for_timeout(500)
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 5: Seleciona Cliente via Select2 ────────────────────────────────
    # Select2 exige interação nativa: abrir dropdown, digitar nome, aguardar AJAX,
    # clicar no resultado — injeção de valor direto no <select> não atualiza a UI
    passo = "Passo 5 — Selecionar Cliente"
    try:
        logger.info("Selecionando cliente: %s (ID: %s)", customer_name, customer_id)
        selecionado_direto = await _select2_selecionar_por_id_ou_nome(
            page,
            select_id="edi_import_batch_customer_id",
            option_value=customer_id,
            option_label=customer_name,
        )
        if not selecionado_direto:
            logger.info("Selecao direta do cliente nao confirmou na UI, usando busca por nome.")
            await _select2_selecionar_por_nome(page, "edi_import_batch_customer_id", customer_name)
        await page.wait_for_timeout(800)  # aguarda filiais carregarem via AJAX
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 6: Seleciona Filial via Select2 ─────────────────────────────────
    passo = "Passo 6 — Selecionar Filial"
    try:
        logger.info("Selecionando filial: %s", corporation_name)
        await page.wait_for_selector('#edi_import_batch_corporation_id', timeout=10000)
        corporation_value = _mapear_corporation_id(corporation_name)
        if corporation_value:
            selecionado_direto = await _select2_selecionar_por_id_ou_nome(
                page,
                select_id="edi_import_batch_corporation_id",
                option_value=corporation_value,
                option_label=corporation_name,
            )
            if not selecionado_direto:
                await _select2_selecionar_por_nome(page, "edi_import_batch_corporation_id", corporation_name)
        else:
            await _select2_selecionar_por_nome(page, "edi_import_batch_corporation_id", corporation_name)
        await page.wait_for_timeout(500)
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 7: Upload dos XMLs ──────────────────────────────────────────────
    # O input[type=file] pode estar oculto pelo plugin bootstrap-fileinput
    # Usar set_input_files direto no input nativo com múltiplos arquivos de uma vez
    passo = "Passo 7 — Upload dos arquivos XML"
    try:
        logger.info("Fazendo upload de %d XML(s)...", len(xml_files))
        upload_payloads = [
            {
                "name": arquivo["name"],
                "mimeType": "application/xml",
                "buffer": base64.b64decode(arquivo["content_base64"]),
            }
            for arquivo in xml_files
        ]

        # Torna o input visível caso esteja oculto pelo bootstrap-fileinput
        await page.evaluate("""
            const el = document.getElementById('edi_import_batch_documents');
            if (el) {
                el.style.display = 'block';
                el.style.opacity = '1';
                el.style.visibility = 'visible';
                el.removeAttribute('hidden');
            }
        """)

        await page.set_input_files('form#edi_import_batch_upload input#edi_import_batch_documents', upload_payloads)
        await _aguardar_upload_xml(page, len(upload_payloads))
        logger.info("Upload realizado: %d arquivo(s)", len(upload_payloads))

    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 8: Clica em Salvar ──────────────────────────────────────────────
    passo = "Passo 8 — Clicar em Salvar"
    try:
        logger.info(passo)
        await page.click('button#submit')
        await page.wait_for_timeout(1000)
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 9: Confirma modal de atenção (se aparecer) ─────────────────────
    passo = "Passo 9 — Confirmar modal de atenção"
    try:
        logger.info("Verificando modal de confirmação...")
        confirmado = False
        for selector in (
            '#swal-confirm',
            'button.swal2-confirm',
            '.swal2-popup button.swal2-confirm',
            '.modal .btn-primary',
            '.modal .btn-confirm',
            '.modal button[data-confirm]',
        ):
            botao = page.locator(selector).first
            try:
                await botao.wait_for(state="visible", timeout=2500)
                await botao.click()
                logger.info("Confirmacao realizada via seletor: %s", selector)
                confirmado = True
                break
            except Exception:
                continue

        if not confirmado:
            logger.info("Nenhum botao de confirmacao apareceu apos salvar.")
    except Exception as e:
        # Modal pode não aparecer — não é erro fatal
        logger.info("Modal de confirmação não apareceu ou não era necessário: %s", e)

    # ── PASSO 10: Trata popup/overlay intermediário ───────────────────────────
    passo = "Passo 10 — Tratar popup intermediario"
    try:
        logger.info(passo)
        await _log_auto_generate_state(page, "antes_de_tratar_popup")
        await _fechar_popup_intermediario(page)
        await page.wait_for_timeout(500)
        await _log_auto_generate_state(page, "depois_de_tratar_popup")
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 11: Garantir "Importar documentos" após a criação do lote ──────
    passo = "Passo 11 — Garantir Importar documentos"
    try:
        logger.info(passo)
        await page.wait_for_selector('#edi_import_batch_auto_generate', state='attached', timeout=15000)
        await _log_auto_generate_state(page, "antes_da_selecao")

        if await _select2_ui_reflete_valor(page, "edi_import_batch_auto_generate", "Importar documentos"):
            logger.info("Processamento Automatico ja estava em 'Importar documentos'.")
        else:
            selecionado = await _select2_selecionar_por_valor_e_texto(
                page,
                select_id="edi_import_batch_auto_generate",
                option_value="only_documents",
                texto="Importar documentos",
            )
            if not selecionado:
                await _select2_selecionar_opcao_visivel(
                    page,
                    select_id="edi_import_batch_auto_generate",
                    texto="Importar documentos",
                )
            await page.wait_for_timeout(500)

        await _log_auto_generate_state(page, "depois_da_selecao")
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 12: Aguardar navegação para a página do lote ───────────────────────
    passo = "Passo 12 — Aguardar página do lote"
    lote = None
    try:
        logger.info(passo)
        await page.wait_for_load_state("networkidle", timeout=30000)
        await page.wait_for_timeout(1000)
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 13: Extrair número do lote ─────────────────────────────────────
    passo = "Passo 13 — Extrair número do lote"
    try:
        logger.info("Aguardando processamento...")

        lote = await page.evaluate(
            """() => {
                const m = window.location.href.match(/batches\\/([0-9]+)/);
                if (m) return m[1];

                for (const sel of ['h1', '.page-title', '.alert-success', '[data-lote]']) {
                    const el = document.querySelector(sel);
                    if (el) {
                        const n = el.textContent.trim().match(/[0-9]{4,}/);
                        if (n) return n[0];
                    }
                }
                return null;
            }"""
        )

        if not lote:
            # ESL Cloud carrega via AJAX — tenta extrair do conteúdo da página
            lote = await page.evaluate(
                """() => {
                    for (const sel of ['h1','h2','h3','h4','.modal-title','.page-title','.panel-title','[data-lote]']) {
                        const el = document.querySelector(sel);
                        if (el) {
                            const m = (el.textContent || '').match(/[0-9]{4,}/);
                            if (m) return m[0];
                        }
                    }
                    for (const form of document.querySelectorAll('form')) {
                        const action = form.getAttribute('action') || '';
                        const m = action.match(/batches\\/([0-9]+)/);
                        if (m) return m[1];
                    }
                    return null;
                }"""
            )

        logger.info("Lote: %s", lote)
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PRÉ-CONDIÇÃO: Verificar check verde (Dados do Lote) ──────────────────
    passo = "Pré-condição — Verificar status dos arquivos"
    try:
        logger.info(passo)
        await page.wait_for_function(
            """() => {
                const icos = Array.from(document.querySelectorAll(
                    '#tab-batch i.fa-check-circle, i.fa.fa-check-circle'
                ));
                if (icos.length === 0) return false;
                return icos.every(i => i.classList.contains('font-green-soft'));
            }""",
            timeout=30000,
        )
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 14: Clicar na aba Documentos Importados ─────────────────────────
    passo = "Passo 14 — Clicar na aba Documentos Importados"
    try:
        logger.info(passo)
        await page.click('a[href="#tab-invoices"]')
        await page.wait_for_timeout(1000)
    except Exception as e:
        return await _erro(page, passo, e)

    # ── VERIFICAÇÃO: Detectar documentos duplicados ───────────────────────────
    passo = "Verificação — Documentos duplicados"
    try:
        duplicados = await page.evaluate(
            """() => {
                const tab = document.querySelector('#tab-invoices');
                if (!tab) return 0;
                const m = (tab.textContent || '').match(/Duplicados\\s*-\\s*(\\d+)/);
                return m ? parseInt(m[1]) : 0;
            }"""
        )
        if duplicados > 0:
            screenshot = await page.screenshot(full_page=False)
            screenshot_b64 = base64.b64encode(screenshot).decode()
            logger.warning("Documentos duplicados detectados: %d", duplicados)
            return {
                "sucesso":    False,
                "lote":       str(lote) if lote else None,
                "documentos": len(xml_files),
                "erro":       f"DUPLICADO ({duplicados} documento(s)) — NF-e já importada anteriormente.",
                "screenshot": screenshot_b64,
            }
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 15: Selecionar todos os documentos ──────────────────────────────
    passo = "Passo 15 — Selecionar todos os documentos"
    try:
        logger.info(passo)
        await page.wait_for_selector(
            '#tab-invoices input[type="checkbox"].toggle.uniform', timeout=10000
        )
        await page.click('#tab-invoices input[type="checkbox"].toggle.uniform')
        await page.wait_for_selector("#tab-invoices .group-actions", state="visible", timeout=5000)
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 16: Clicar em Processar ────────────────────────────────────────
    passo = "Passo 16 — Clicar em Processar"
    try:
        logger.info(passo)
        clicou = await page.evaluate(
            """() => {
                const btns = Array.from(document.querySelectorAll('.group-actions a.btn'));
                const btn = btns.find(b => b.querySelector('i.fa-file-import'));
                if (btn) { btn.click(); return true; }
                return false;
            }"""
        )
        if not clicou:
            raise RuntimeError("Botão 'Processar' não encontrado na barra de ações.")
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 17: Confirmar modal "Confirma geração dos fretes?" ─────────────
    passo = "Passo 17 — Confirmar geração dos fretes"
    try:
        logger.info(passo)
        await page.wait_for_selector(
            "button.swal2-confirm.swal2-styled", state="visible", timeout=10000
        )
        await page.click("button.swal2-confirm.swal2-styled")
        await page.wait_for_timeout(1000)
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 18: Verificar se todos os documentos estão em "Processados" ─────
    passo = "Passo 18 — Verificar documentos processados"
    try:
        logger.info(passo)
        await page.wait_for_function(
            """() => {
                const tab = document.querySelector('#tab-invoices');
                if (!tab) return false;
                const txt = tab.textContent || '';
                const pendentes   = parseInt(txt.match(/Pendentes\\s*-\\s*(\\d+)/)?.[1]   || '1');
                const processados = parseInt(txt.match(/Processados\\s*-\\s*(\\d+)/)?.[1] || '0');
                return pendentes === 0 && processados > 0;
            }""",
            timeout=120000,
        )
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 19: Clicar na aba Fretes ───────────────────────────────────────
    passo = "Passo 19 — Clicar na aba Fretes"
    try:
        logger.info(passo)
        await page.click('a[href="#tab-freights"]')
        await page.wait_for_timeout(1000)
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 20: Selecionar todos os fretes ─────────────────────────────────
    passo = "Passo 20 — Selecionar todos os fretes"
    try:
        logger.info(passo)
        await page.wait_for_selector(
            '#tab-freights input[type="checkbox"].toggle.uniform', timeout=10000
        )
        await page.click('#tab-freights input[type="checkbox"].toggle.uniform')
        await page.wait_for_selector("#tab-freights .group-actions", state="visible", timeout=5000)
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 21: Clicar em "Gerar CT-es" ────────────────────────────────────
    passo = "Passo 21 — Clicar em Gerar CT-es"
    try:
        logger.info(passo)
        clicou = await page.evaluate(
            """() => {
                const btns = Array.from(document.querySelectorAll('.group-actions a.btn'));
                const btn = btns.find(b => {
                    const span = b.querySelector('span.text-icon');
                    return span && span.textContent.trim() === 'Gerar CT-es';
                });
                if (btn) { btn.click(); return true; }
                return false;
            }"""
        )
        if not clicou:
            raise RuntimeError("Botão 'Gerar CT-es' não encontrado na barra de ações.")
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 22: Confirmar modal "Confirma a emissão dos CT-es?" ─────────────
    passo = "Passo 22 — Confirmar emissão dos CT-es"
    try:
        logger.info(passo)
        await page.wait_for_selector(
            "button.swal2-confirm.swal2-styled", state="visible", timeout=10000
        )
        await page.click("button.swal2-confirm.swal2-styled")
        await page.wait_for_timeout(1000)
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 23: Aguardar spinner desaparecer ────────────────────────────────
    passo = "Passo 23 — Aguardar processamento dos CT-es"
    try:
        logger.info(passo)
        await page.wait_for_function(
            """() => {
                const spinner = document.querySelector(
                    '#tab-freights tbody .fa-spinner, #tab-freights tbody .fa-spin'
                );
                return spinner === null;
            }""",
            timeout=120000,
        )
    except Exception as e:
        return await _erro(page, passo, e)

    # ── PASSO 24: Clicar em "Autorizados" e verificar status final ───────────
    passo = "Passo 24 — Verificar status final dos CT-es"
    try:
        logger.info(passo)
        await page.evaluate(
            """() => {
                const btns = Array.from(document.querySelectorAll('#tab-freights .btn.btn-sticky'));
                const btn = btns.find(b => b.textContent.trim().startsWith('Autorizados'));
                if (btn) btn.click();
            }"""
        )
        await page.wait_for_timeout(1000)

        status_cte = await page.evaluate(
            """() => {
                const tab = document.querySelector('#tab-freights');
                if (!tab) return null;
                const txt = tab.textContent || '';
                const autorizados    = parseInt(txt.match(/Autorizados\\s*-\\s*(\\d+)/)?.[1]    || 0);
                const inconsistentes = parseInt(txt.match(/Inconsistentes\\s*-\\s*(\\d+)/)?.[1] || 0);
                const rejeitados     = parseInt(txt.match(/Rejeitados\\s*-\\s*(\\d+)/)?.[1]     || 0);
                const pendentes      = parseInt(txt.match(/Pendentes\\s*-\\s*(\\d+)/)?.[1]      || 0);

                if (autorizados > 0 && inconsistentes === 0 && rejeitados === 0) {
                    return 'OK';
                } else if (inconsistentes > 0) {
                    return `INCONSISTENTE (${inconsistentes} frete(s))`;
                } else if (rejeitados > 0) {
                    return `REJEITADO (${rejeitados} frete(s))`;
                } else if (pendentes > 0) {
                    return `PENDENTE (${pendentes} frete(s))`;
                }
                return null;
            }"""
        )
        logger.info("Status final CT-es: %s", status_cte)

        screenshot = await page.screenshot(full_page=False)
        screenshot_b64 = base64.b64encode(screenshot).decode()
        return {
            "sucesso":    True,
            "lote":       str(lote) if lote else None,
            "documentos": len(xml_files),
            "status_cte": status_cte,
            "screenshot": screenshot_b64,
        }

    except Exception as e:
        return await _erro(page, passo, e)


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _select2_selecionar_por_id_ou_nome(page, select_id: str, option_value: str, option_label: str) -> bool:
    option_value = str(option_value or "").strip()
    option_label = str(option_label or "").strip()
    if not option_value or not option_label:
        return False

    selecionado = await page.evaluate(
        """({ selectId, optionValue, optionLabel }) => {
            const select = document.getElementById(selectId);
            if (!select) return false;

            let option = Array.from(select.options).find((item) => item.value === optionValue);
            if (!option) {
                option = new Option(optionLabel, optionValue, true, true);
                select.add(option);
            } else {
                option.text = optionLabel;
                option.selected = true;
            }

            select.value = optionValue;
            select.dispatchEvent(new Event('input', { bubbles: true }));
            select.dispatchEvent(new Event('change', { bubbles: true }));

            if (window.jQuery) {
                const $select = window.jQuery(select);
                $select.val(optionValue).trigger('change');
                if (window.jQuery.fn && window.jQuery.fn.select2) {
                    $select.trigger({
                        type: 'select2:select',
                        params: { data: { id: optionValue, text: optionLabel } }
                    });
                }
            }

            return true;
        }""",
        {
            "selectId": select_id,
            "optionValue": option_value,
            "optionLabel": option_label,
        },
    )
    if not selecionado:
        return False

    await page.wait_for_timeout(1200)
    return await _select2_ui_reflete_valor(page, select_id, option_label)


async def _select2_selecionar_por_nome(page, select_id: str, texto: str):
    """
    Abre um Select2, escreve o nome visivel e seleciona a opcao encontrada.
    Esse fluxo replica o mapeamento capturado pelo Playwright Inspector.
    """
    texto = (texto or "").strip()
    if not texto:
        raise RuntimeError(f"Texto vazio para selecionar em {select_id}")

    container_selector = f"#upload-modal #select2-{select_id}-container"
    form_selector = f"#edi_import_batch_upload #select2-{select_id}-container"
    upload_data_selector = f"#upload_data #select2-{select_id}-container"
    fallback_selector = f"#select2-{select_id}-container"

    trigger = page.locator(container_selector).first
    if await trigger.count() == 0:
        trigger = page.locator(form_selector).first
    if await trigger.count() == 0:
        trigger = page.locator(upload_data_selector).first
    if await trigger.count() == 0:
        trigger = page.locator(fallback_selector).first
    if await trigger.count() == 0:
        raise RuntimeError(f"Select2 container nao encontrado para: {select_id}")

    await trigger.click()

    search = page.locator('input[type="search"]').last
    await search.wait_for(state="visible", timeout=5000)
    await search.click()

    # Digita por palavras e espera o dropdown reagir entre uma etapa e outra.
    await search.fill("")
    palavras = [parte for parte in texto.split() if parte]
    acumulado = []
    for indice, palavra in enumerate(palavras, start=1):
        acumulado.append(palavra)
        await search.fill(" ".join(acumulado))
        await page.wait_for_timeout(3000 if indice == 1 else 5000)
        if await _select2_tem_opcao(page, texto):
            break

    await page.wait_for_selector(
        '[role="treeitem"], .select2-results__option:not(.select2-results__option--disabled)',
        state="visible",
        timeout=10000,
    )

    selecionou = await page.evaluate(
        """(texto) => {
            const normalizar = (valor) => String(valor || '')
                .normalize('NFD')
                .replace(/[\\u0300-\\u036f]/g, '')
                .replace(/\\s+/g, ' ')
                .trim()
                .toUpperCase();

            const alvo = normalizar(texto);
            const opcoes = Array.from(document.querySelectorAll(
                '[role="treeitem"], .select2-results__option:not(.select2-results__option--disabled)'
            )).filter((el) => {
                const txt = normalizar(el.textContent);
                return txt && !txt.includes('SEARCHING') && !txt.includes('CARREGANDO');
            });

            const candidatos = [
                opcoes.find((el) => normalizar(el.textContent) === alvo),
                opcoes.find((el) => normalizar(el.textContent).includes(alvo)),
                opcoes.find((el) => alvo.includes(normalizar(el.textContent))),
                opcoes[0],
            ];
            const escolhido = candidatos.find(Boolean);
            if (!escolhido) return null;
            escolhido.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
            escolhido.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
            escolhido.click();
            return escolhido.textContent.trim();
        }""",
        texto,
    )

    if not selecionou:
        raise RuntimeError(f"Nenhuma opcao encontrada no Select2 para: {texto}")

    logger.info("Select2 %s selecionado por nome: %s", select_id, selecionou)


async def _select2_tem_opcao(page, texto: str) -> bool:
    return await page.evaluate(
        """(texto) => {
            const normalizar = (valor) => String(valor || '')
                .normalize('NFD')
                .replace(/[\\u0300-\\u036f]/g, '')
                .replace(/\\s+/g, ' ')
                .trim()
                .toUpperCase();

            const alvo = normalizar(texto);
            const opcoes = Array.from(document.querySelectorAll(
                '[role="treeitem"], .select2-results__option:not(.select2-results__option--disabled)'
            ));
            return opcoes.some((el) => normalizar(el.textContent) === alvo);
        }""",
        texto,
    )


async def _select2_selecionar_opcao_visivel(page, select_id: str, texto: str):
    texto = (texto or "").strip()
    if not texto:
        raise RuntimeError(f"Texto vazio para selecionar em {select_id}")

    container_selector = f"#upload-modal #select2-{select_id}-container"
    form_selector = f"#edi_import_batch_upload #select2-{select_id}-container"
    fallback_selector = f"#select2-{select_id}-container"

    trigger = page.locator(container_selector).first
    if await trigger.count() == 0:
        trigger = page.locator(form_selector).first
    if await trigger.count() == 0:
        trigger = page.locator(fallback_selector).first
    if await trigger.count() == 0:
        raise RuntimeError(f"Select2 container nao encontrado para: {select_id}")

    await trigger.click()

    await page.wait_for_selector(
        '.select2-results__option:not(.select2-results__option--disabled), [role="treeitem"]',
        state="visible",
        timeout=10000,
    )

    selecionou = await page.evaluate(
        """(texto) => {
            const normalizar = (valor) => String(valor || '')
                .normalize('NFD')
                .replace(/[\\u0300-\\u036f]/g, '')
                .replace(/\\s+/g, ' ')
                .trim()
                .toUpperCase();

            const alvo = normalizar(texto);
            const opcoes = Array.from(document.querySelectorAll(
                '.select2-results__option:not(.select2-results__option--disabled), [role="treeitem"]'
            )).filter((el) => {
                const txt = normalizar(el.textContent);
                return txt && !txt.includes('SEARCHING') && !txt.includes('CARREGANDO');
            });

            const opcao = opcoes.find((el) => normalizar(el.textContent) === alvo)
                || opcoes.find((el) => normalizar(el.textContent).includes(alvo));

            if (!opcao) return null;
            opcao.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
            opcao.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
            opcao.click();
            return opcao.textContent.trim();
        }""",
        texto,
    )

    if not selecionou:
        raise RuntimeError(f"Nao foi possivel selecionar a opcao visivel '{texto}' em {select_id}")

    if not await _select2_ui_reflete_valor(page, select_id, texto):
        raise RuntimeError(f"A UI do Select2 nao refletiu o valor '{texto}' em {select_id}")

    logger.info("Select2 %s selecionado por opcao visivel: %s", select_id, selecionou)


async def _select2_selecionar_option_por_texto(page, select_id: str, texto: str) -> bool:
    selecionado = await page.evaluate(
        """({ selectId, texto }) => {
            const normalizar = (valor) => String(valor || '')
                .normalize('NFD')
                .replace(/[\\u0300-\\u036f]/g, '')
                .replace(/\\s+/g, ' ')
                .trim()
                .toUpperCase();

            const select = document.getElementById(selectId);
            if (!select) return false;

            const alvo = normalizar(texto);
            const option = Array.from(select.options).find((item) => normalizar(item.textContent || item.text) === alvo);
            if (!option) return false;

            option.selected = true;
            select.value = option.value;
            select.dispatchEvent(new Event('input', { bubbles: true }));
            select.dispatchEvent(new Event('change', { bubbles: true }));

            if (window.jQuery) {
                const $select = window.jQuery(select);
                $select.val(option.value).trigger('change');
                $select.trigger({
                    type: 'select2:select',
                    params: { data: { id: option.value, text: option.text } }
                });
            }

            return true;
        }""",
        {"selectId": select_id, "texto": texto},
    )
    if not selecionado:
        return False

    await page.wait_for_timeout(400)
    refletiu = await _select2_ui_reflete_valor(page, select_id, texto)
    if refletiu:
        logger.info("Select2 %s selecionado pela option raiz: %s", select_id, texto)
    return refletiu


async def _select2_selecionar_por_valor_e_texto(page, select_id: str, option_value: str, texto: str) -> bool:
    selecionado = await page.evaluate(
        """({ selectId, optionValue, texto }) => {
            const normalizar = (valor) => String(valor || '')
                .normalize('NFD')
                .replace(/[\\u0300-\\u036f]/g, '')
                .replace(/\\s+/g, ' ')
                .trim()
                .toUpperCase();

            const select = document.getElementById(selectId);
            if (!select) return { ok: false, erro: 'select_nao_encontrado' };

            const option = Array.from(select.options).find((item) => String(item.value || '').trim() === optionValue);
            if (!option) {
                return {
                    ok: false,
                    erro: 'option_nao_encontrada',
                    options: Array.from(select.options).map((item) => ({ value: item.value, text: item.text }))
                };
            }

            option.selected = true;
            select.value = option.value;
            select.dispatchEvent(new Event('input', { bubbles: true }));
            select.dispatchEvent(new Event('change', { bubbles: true }));

            if (window.jQuery) {
                const $select = window.jQuery(select);
                $select.val(option.value).trigger('change');
                $select.trigger({
                    type: 'select2:select',
                    params: { data: { id: option.value, text: option.text } }
                });
            }

            const container = document.querySelector(`#select2-${selectId}-container`);
            return {
                ok: true,
                value: option.value,
                text: option.text,
                rendered: container ? container.textContent : ''
            };
        }""",
        {"selectId": select_id, "optionValue": option_value, "texto": texto},
    )
    if not selecionado.get("ok"):
        logger.warning("Falha ao selecionar %s por valor %s: %s", select_id, option_value, selecionado)
        return False

    await page.wait_for_timeout(500)
    refletiu = await _select2_ui_reflete_valor(page, select_id, texto)
    if refletiu:
        logger.info(
            "Select2 %s selecionado pela option raiz com value=%s e texto=%s",
            select_id,
            option_value,
            texto,
        )
    else:
        logger.warning(
            "Select2 %s recebeu value=%s, mas a UI nao refletiu %s",
            select_id,
            option_value,
            texto,
        )
    return refletiu


def _mapear_corporation_id(corporation_name: str) -> str | None:
    normalizado = _normalizar_texto(corporation_name)
    mapa = {
        "MANDALOG - BAURU": "153564",
        "MANDALOG OPERACOES LOGISTICAS": "29697",
    }
    return mapa.get(normalizado)


def _normalizar_texto(valor: str) -> str:
    return " ".join(str(valor or "").strip().upper().split())


async def _validar_modal_importacao(page):
    valido = await page.evaluate(
        """() => {
            const form = document.getElementById('edi_import_batch_upload');
            if (!form) return { ok: false, erro: 'Formulario do modal nao encontrado.' };

            const campoFormato = document.getElementById('edi_import_batch_file_format');
            const campoDocumento = document.getElementById('edi_import_batch_document_type');
            const campoCliente = document.getElementById('edi_import_batch_customer_id');
            const campoFilial = document.getElementById('edi_import_batch_corporation_id');
            const campoArquivos = document.getElementById('edi_import_batch_documents');

            if (!campoCliente || !campoFilial || !campoArquivos) {
                return { ok: false, erro: 'Campos principais do modal nao estao presentes.' };
            }

            const formato = campoFormato ? String(campoFormato.value || '').trim().toLowerCase() : '';
            const documento = campoDocumento ? String(campoDocumento.value || '').trim().toLowerCase() : '';

            if (formato && formato !== 'xml') {
                return { ok: false, erro: `Modal abriu com file_format inesperado: ${formato}` };
            }
            if (documento && documento !== 'invoice') {
                return { ok: false, erro: `Modal abriu com document_type inesperado: ${documento}` };
            }

            return { ok: true };
        }""",
    )
    if not valido.get("ok"):
        raise RuntimeError(valido.get("erro") or "Modal de importacao invalido.")


async def _select2_ui_reflete_valor(page, select_id: str, texto: str) -> bool:
    return await page.evaluate(
        """({ selectId, texto }) => {
            const normalizar = (valor) => String(valor || '')
                .normalize('NFD')
                .replace(/[\\u0300-\\u036f]/g, '')
                .replace(/\\s+/g, ' ')
                .trim()
                .toUpperCase();

            const alvo = normalizar(texto);
            const container = document.querySelector(`#select2-${selectId}-container`);
            const rendered = container ? normalizar(container.textContent) : '';
            const select = document.getElementById(selectId);
            const valor = select ? String(select.value || '').trim() : '';

            return rendered === alvo || rendered.includes(alvo) || Boolean(valor);
        }""",
        {
            "selectId": select_id,
            "texto": texto,
        },
    )


async def _aguardar_upload_xml(page, quantidade_arquivos: int):
    try:
        await page.wait_for_function(
            """(quantidade) => {
                const input = document.getElementById('edi_import_batch_documents');
                const selecionados = input?.files?.length || 0;

                const uploadsPendentes = document.querySelectorAll(
                    '.direct-upload--pending, .direct-upload--progress, .file-preview-status.text-warning, .kv-upload-progress .progress-bar'
                ).length;

                const arquivosOcultos = document.querySelectorAll(
                    'input[type="hidden"][name="edi_import_batch[documents][]"]'
                ).length;

                const nomesArquivos = document.querySelectorAll(
                    '.file-preview .file-footer-caption, .file-preview .file-caption-name, .kv-file-content'
                ).length;

                return (
                    selecionados >= quantidade &&
                    uploadsPendentes === 0 &&
                    (arquivosOcultos >= quantidade || nomesArquivos >= quantidade)
                );
            }""",
            quantidade_arquivos,
            timeout=20000,
        )
    except Exception:
        logger.warning("Nao foi possivel confirmar o upload pela UI; aguardando tolerancia extra.")
        await page.wait_for_timeout(5000)

    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass


async def _fechar_popup_intermediario(page):
    await page.wait_for_timeout(1200)

    try:
        toast = await page.locator(".toast-message").first.text_content(timeout=1500)
        if toast:
            logger.warning("Toast visivel antes do auto_generate: %s", toast.strip())
    except Exception:
        pass

    # Trata explicitamente o backdrop observado nessa tela.
    try:
        backdrop = page.locator(".modal-backdrop.fade.in").first
        if await backdrop.count() > 0:
            await backdrop.click(force=True, timeout=2000)
            logger.info("Clique realizado em .modal-backdrop.fade.in")
            await page.wait_for_timeout(1000)
    except Exception as e:
        logger.info("Nao foi possivel clicar diretamente no modal-backdrop: %s", e)

    for _ in range(4):
        houve_popup = await page.evaluate(
            """() => {
                const visivel = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && style.opacity !== '0'
                        && rect.width > 0
                        && rect.height > 0;
                };

                const candidatos = [
                    ...document.querySelectorAll('.swal2-container, .swal2-popup, .modal-backdrop, .bootbox, .ui-widget-overlay'),
                    ...document.querySelectorAll('div[role="dialog"], div.modal, div[class*="overlay"], div[class*="backdrop"]')
                ].filter(visivel);

                const popup = candidatos.find((el) => {
                    const rect = el.getBoundingClientRect();
                    return rect.width >= 120 && rect.height >= 60;
                });

                if (!popup) return { houvePopup: false };

                const rect = popup.getBoundingClientRect();
                const x = rect.left + rect.width / 2;
                const y = rect.top + rect.height / 2;
                const alvo = document.elementFromPoint(x, y) || popup;

                alvo.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: x, clientY: y }));
                alvo.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: x, clientY: y }));
                alvo.dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: x, clientY: y }));
                return {
                    houvePopup: true,
                    tag: popup.tagName,
                    classes: popup.className || '',
                    width: Math.round(rect.width),
                    height: Math.round(rect.height)
                };
            }""",
        )
        logger.info("Diagnostico popup intermediario: %s", houve_popup)
        if not houve_popup.get("houvePopup"):
            break
        await page.wait_for_timeout(1200)

    try:
        await page.wait_for_function(
            """() => {
                const visivel = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && style.opacity !== '0'
                        && rect.width > 0
                        && rect.height > 0;
                };

                const bloqueios = [
                    ...document.querySelectorAll('.swal2-container, .swal2-popup, .modal-backdrop, .bootbox, .ui-widget-overlay'),
                    ...document.querySelectorAll('div[role="dialog"], div.modal, div[class*="overlay"], div[class*="backdrop"]')
                ].filter(visivel);

                return bloqueios.length === 0;
            }""",
            timeout=10000,
        )
    except Exception:
        logger.info("Nao foi detectado popup bloqueante persistente ou o overlay nao exigia fechamento.")

    try:
        toast = await page.locator(".toast-message").first.text_content(timeout=1000)
        if toast:
            logger.warning("Toast ainda visivel apos tratar popup: %s", toast.strip())
    except Exception:
        pass


async def _aguardar_campo_interativo(page, selector: str):
    await page.wait_for_function(
        """(sel) => {
            const el = document.querySelector(sel);
            if (!el) return false;
            if (el.disabled) return false;

            const rect = el.getBoundingClientRect();
            if (!rect.width || !rect.height) return false;

            const x = rect.left + Math.max(1, Math.min(rect.width / 2, rect.width - 1));
            const y = rect.top + Math.max(1, Math.min(rect.height / 2, rect.height - 1));
            const topo = document.elementFromPoint(x, y);

            return Boolean(topo);
        }""",
        selector,
        timeout=10000,
    )


async def _log_auto_generate_state(page, etapa: str):
    diagnostico = await page.evaluate(
        """() => {
            const select = document.getElementById('edi_import_batch_auto_generate');
            const container = document.getElementById('select2-edi_import_batch_auto_generate-container');
            const visivel = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && style.opacity !== '0'
                    && rect.width > 0
                    && rect.height > 0;
            };

            const overlays = [
                ...document.querySelectorAll('.swal2-container, .swal2-popup, .modal-backdrop, .bootbox, .ui-widget-overlay'),
                ...document.querySelectorAll('div[role="dialog"], div.modal, div[class*="overlay"], div[class*="backdrop"]')
            ].filter(visivel).map((el) => {
                const rect = el.getBoundingClientRect();
                return {
                    tag: el.tagName,
                    classes: el.className || '',
                    width: Math.round(rect.width),
                    height: Math.round(rect.height)
                };
            });

            return {
                selectExiste: Boolean(select),
                selectDisabled: select ? Boolean(select.disabled) : null,
                selectValue: select ? String(select.value || '') : null,
                selectOptions: select ? Array.from(select.options).map((item) => ({
                    value: item.value,
                    text: item.text,
                    selected: item.selected
                })) : [],
                renderedText: container ? container.textContent.trim() : null,
                renderedTitle: container ? (container.getAttribute('title') || '') : null,
                overlays
            };
        }""",
    )
    logger.info("Diagnostico auto_generate [%s]: %s", etapa, diagnostico)


def _salvar_xmls_recebidos(xml_files: list) -> list[str]:
    pasta_base = os.path.join(os.path.dirname(__file__), "data", "xml_recebidos")
    lote = datetime.now().strftime("%Y%m%d_%H%M%S")
    pasta_lote = os.path.join(pasta_base, lote)
    os.makedirs(pasta_lote, exist_ok=True)

    caminhos = []
    for indice, arquivo in enumerate(xml_files, start=1):
        nome_original = str(arquivo.get("name") or f"documento_{indice}.xml").strip()
        nome_seguro = os.path.basename(nome_original) or f"documento_{indice}.xml"
        xml_bytes = base64.b64decode(arquivo["content_base64"])

        caminho_xml = os.path.join(pasta_lote, nome_seguro)
        with open(caminho_xml, "wb") as f:
            f.write(xml_bytes)
        caminhos.append(caminho_xml)

        hash_sha256 = hashlib.sha256(xml_bytes).hexdigest()
        with open(f"{caminho_xml}.sha256.txt", "w", encoding="ascii") as f:
            f.write(hash_sha256)

    return caminhos


async def _select2_selecionar(page, select_id: str, texto: str):
    """Interage com Select2: abre o dropdown, digita o texto, aguarda AJAX e clica no primeiro resultado."""
    # Clica no trigger do Select2 (o <span> que substitui o <select> original)
    await page.evaluate("""(id) => {
        const sel = document.getElementById(id);
        if (!sel) throw new Error('Select não encontrado: ' + id);
        const container = sel.nextElementSibling;
        const trigger = container && container.querySelector('.select2-selection');
        if (!trigger) throw new Error('Select2 trigger não encontrado para: ' + id);
        trigger.click();
    }""", select_id)

    await page.wait_for_selector('.select2-dropdown .select2-search__field', state='visible', timeout=5000)
    await page.fill('.select2-dropdown .select2-search__field', texto)
    await page.wait_for_timeout(1200)  # aguarda resposta AJAX

    await page.wait_for_selector(
        '.select2-results__option:not(.select2-results__option--disabled)',
        state='visible',
        timeout=10000,
    )
    await page.locator('.select2-results__option:not(.select2-results__option--disabled)').first().click()


async def _erro(page, passo: str, excecao: Exception) -> dict:
    logger.error("ERRO em [%s]: %s", passo, excecao)
    return {
        "sucesso":    False,
        "erro":       str(excecao),
        "passo":      passo,
        "screenshot": await _screenshot_seguro(page),
    }


async def _screenshot_seguro(page) -> str | None:
    if page is None:
        return None
    try:
        s = await page.screenshot(full_page=False)
        return base64.b64encode(s).decode()
    except Exception:
        return None


def _e_erro_de_sessao(mensagem: str, page) -> bool:
    palavras = ["sign_in", "login", "sessão", "session", "unauthorized", "401"]
    for p in palavras:
        if p.lower() in mensagem.lower():
            return True
    if page:
        try:
            if "/users/sign_in" in page.url:
                return True
        except Exception:
            pass
    return False
