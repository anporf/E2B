import React, { useEffect, useState } from 'react';
import { XML_API } from '@src/api/api.clients';
import { ExportTable } from '../export-xml/export-table';
import { useDispatch, useSelector } from 'react-redux';
import {
    Box,
    Fab,
} from '@mui/material';
import {
    casesListSelector,
    getCasesList,
    setCases,
} from '@src/features/cases-list/slice';
import {
    deleteReport,
    getData,
    setOpenNewReport,
    setShowCasesList,
} from '@src/features/display/slice';
import { HotTable } from '@handsontable/react';
import { registerAllModules } from 'handsontable/registry';
import 'handsontable/dist/handsontable.full.min.css';
import { textRenderer } from 'handsontable/renderers/textRenderer';
import { getMeddraReleases } from '@src/features/meddra/slice';
import FileDownloadIcon from '@mui/icons-material/FileDownload';

registerAllModules();
const hotTableRef = React.createRef();

export const CasesList = () => {
    const dispatch = useDispatch();
    const { cases } = useSelector(casesListSelector);
    const [selectedCases, setSelectedCases] = useState({});
    const [exportDialogOpen, setExportDialogOpen] = useState(false);
    const [infoDialogOpen, setInfoDialogOpen] = useState(false);
    const [currentInfoCase, setCurrentInfoCase] = useState(null);
    const [exportingList, setExportingList] = useState([]);
    const [isValidating, setIsValidating] = useState(false);

    useEffect(() => {
        dispatch(getCasesList());
    }, []);

    const openReport = (id) => {
        dispatch(getData(id));
        dispatch(setOpenNewReport(true));
        dispatch(getMeddraReleases());
        dispatch(setShowCasesList(false));
    };

    const removeReport = (id) => {
        let answer = window.confirm(
            `Are you sure you want to remove report ${id}?`,
        );
        if (!answer) return;

        let casesCopy = JSON.parse(JSON.stringify(cases));
        casesCopy = casesCopy.filter((x) => x.id !== id);
        dispatch(setCases(casesCopy));
        dispatch(deleteReport(id));
        
        // Удаляем выбранный кейс из списка выбранных
        const newSelectedCases = { ...selectedCases };
        delete newSelectedCases[id];
        setSelectedCases(newSelectedCases);
    };

    const toggleCaseSelection = (id) => {
        const newSelectedCases = {
            ...selectedCases,
            [id]: !selectedCases[id]
        };
        
        setSelectedCases(newSelectedCases);
    };

    const openExportDialog = async () => {
        const selectedIds = Object.keys(selectedCases).filter(id => selectedCases[id]);
        
        if (selectedIds.length === 0) {
            return;
        }
        
        setIsValidating(true);
        
        try {
            const selectedCasesData = [];
            
            selectedIds.forEach(id => {
                const caseObj = cases.find(c => String(c.id) === id);
                if (caseObj) {
                    selectedCasesData.push({
                        id: String(caseObj.id),
                        display_id: String(caseObj.id),
                        case_number: caseObj.case_number || String(caseObj.id),
                        isValid: null,
                        missingFields: []
                    });
                }
            });
            
            console.log("Initial selected cases data:", selectedCasesData);
            
            setExportingList(selectedCasesData);
            setExportDialogOpen(true);
            
            const caseIds = selectedCasesData.map(item => item.id);
            console.log("Calling validateExportXML with caseIds:", caseIds);
            
            const validationResults = await XML_API.validateExportXML(caseIds);
            console.log("Validation results received:", validationResults);
            
            const validatedCasesData = selectedCasesData.map((caseItem, index) => {
                const caseValidation = validationResults[index] || {};
                const allFieldErrors = [];
                
                console.log(`Processing validation for case ${caseItem.id}:`, caseValidation);
                
                Object.entries(caseValidation).forEach(([externalKey, fieldErrors]) => {
                    console.log(`Processing external key ${externalKey}:`, fieldErrors);
                    
                    Object.entries(fieldErrors).forEach(([fieldId, errorMessage]) => {
                        allFieldErrors.push({
                            id: fieldId,
                            label: fieldId,
                            description: String(errorMessage),
                            externalKey: externalKey
                        });
                    });
                });
                
                console.log(`Field errors for case ${caseItem.id}:`, allFieldErrors);
                
                return {
                    ...caseItem,
                    isValid: allFieldErrors.length === 0,
                    missingFields: allFieldErrors
                };
            });
            
            console.log("Final validated cases data:", validatedCasesData);
            
            setExportingList(validatedCasesData);
        } catch (error) {
            console.error("Error validating export cases:", error);
            // Если произошла ошибка, можно показать сообщение
            if (typeof enqueueSnackbar === 'function') {
                enqueueSnackbar(`Error validating cases: ${error.message}`, { variant: 'error' });
            }
        } finally {
            setIsValidating(false);
        }
    };
      
    
    const closeExportDialog = () => {
        setExportDialogOpen(false);
    };
    
    const openInfoDialog = (caseId) => {
        setCurrentInfoCase(caseId);
        setInfoDialogOpen(true);
    };
    
    const closeInfoDialog = () => {
        setInfoDialogOpen(false);
    };
    
    const excludeFromExport = (caseId) => {
        setExportingList(prev => prev.filter(item => item.id !== caseId));
        
        if (exportingList.length <= 1) {
            closeExportDialog();
        }
    };
    
    const exportXML = () => {
        console.log("Exporting XML for cases:", exportingList);
        
        try {
            const ids = exportingList.map(item => item.id);
            
            XML_API.exportMultipleXml(ids)
                .then(blob => {
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = `icsr_export_${new Date().toISOString().slice(0, 10)}.xml`;
                    document.body.appendChild(a);
                    a.click();
                    URL.revokeObjectURL(url);
                    document.body.removeChild(a);
                    
                    closeExportDialog();
                })
                .catch(error => {
                    console.error("Error exporting XML:", error);
                });
        } catch (error) {
            console.error("Error preparing export:", error);
        }
    };
    

    function openBtn(instance, td, row, col, prop, value, cellProperties) {
        textRenderer.apply(this, arguments);
        td.innerHTML = '<button class="myBtOpen">OPEN</button>';
    }

    function removeBtn(instance, td, row, col, prop, value, cellProperties) {
        textRenderer.apply(this, arguments);
        td.innerHTML = '<button class="myBtRemove"><i></i></button>';
    }

    function checkboxRenderer(instance, td, row, col, prop, value, cellProperties) {
        textRenderer.apply(this, arguments);
        
        const rowData = instance.getDataAtRow(row);
        const id = rowData[2]; // Это должен быть ID в третьей колонке (индекс 2)
        
        console.log("Rendering checkbox for row", row, "with ID", id);
        
        const isChecked = selectedCases[id] ? 'checked' : '';
        
        td.innerHTML = `<div class="checkbox-wrapper"><input type="checkbox" ${isChecked} class="select-case-checkbox" data-id="${id}" /></div>`;
        
        const checkbox = td.querySelector('.select-case-checkbox');
        checkbox.addEventListener('change', (e) => {
            e.stopPropagation(); // Предотвращаем всплытие события
            const caseId = e.target.getAttribute('data-id');
            console.log("Checkbox changed for ID", caseId);
            toggleCaseSelection(caseId);
        });
    }

    const generateList = () => {
        console.log("Cases data:", cases);
        console.log("Selected cases:", selectedCases);
        return (
            <HotTable
                ref={hotTableRef}
                autoRowSize={true}
                autoColumnSize={true}
                licenseKey="non-commercial-and-evaluation"
                data={cases}
                rowHeights={40}
                style={{ marginLeft: '50px' }}
                rowHeaders={true}
                className="customFilterButton"
                dropdownMenu={{
                    items: {
                        filter_by_condition: {},
                        filter_operators: {},
                        filter_by_condition2: {},
                        filter_by_value: {},
                        filter_action_bar: {},
                    },
                }}
                colHeaders={[
                    'Export', 
                    'Open ',
                    'Id  ',
                    'Case Number  ',
                    'Reaction Country  ',
                    'Serious ',
                    'Creation date',
                    'Received date',
                    'Suspect Drug Name  ',
                    'Reaction MedDRA Code  ',
                    'Remove ',
                ]}
                columns={[
                    { editor: false },
                    { editor: false },
                    { data: 'id', editor: false },
                    { data: 'case_number', editor: false },
                    { data: 'country', editor: false },
                    { data: 'serious', editor: false },
                    { data: 'creation_date', editor: false },
                    { data: 'received_date', editor: false },
                    { data: 'drug_names', editor: false },
                    { data: 'reaction_names', editor: false },
                    { editor: false },
                ]}
                manualColumnResize={true}
                filters={true}
                columnSorting={true}
                columnHeaderHeight={35}
                stretchH="all"
                cells={function (row, col) {
                    var cellPrp = {};
                    if (col === 0) {
                        cellPrp.renderer = checkboxRenderer;
                        cellPrp.readOnly = true;
                    }
                    if (col === 1) {
                        cellPrp.renderer = openBtn;
                        cellPrp.readOnly = true;
                    }
                    if (col === 10) {
                        cellPrp.renderer = removeBtn;
                        cellPrp.readOnly = true;
                    }
                    return cellPrp;
                }}
                afterOnCellMouseDown={function (event, cords, TD) {
                    if (cords['row'] === -1) return;
                    
                    if (cords['col'] === 1) {
                        const id = hotTableRef.current.hotInstance.getDataAtRow(
                            cords['row'],
                        )[2];
                        openReport(id);
                    }
                    if (cords['col'] === 10) {
                        const id = hotTableRef.current.hotInstance.getDataAtRow(
                            cords['row'],
                        )[2];
                        removeReport(id);
                    }
                }}
            ></HotTable>
        );
    };

    const hasSelectedCases = Object.values(selectedCases).some(selected => selected);

    return (
        <Box sx={{ position: 'relative', height: '100%' }}>
            {generateList()}
            
            {hasSelectedCases && (
                <Fab
                    color="primary"
                    variant="extended"
                    sx={{
                        position: 'fixed',
                        bottom: 16,
                        right: 16,
                        zIndex: 1000
                    }}
                    onClick={openExportDialog}
                >
                    <FileDownloadIcon sx={{ mr: 1 }} />
                    Export XML ({Object.values(selectedCases).filter(Boolean).length})
                </Fab>
            )}
            
            <ExportTable 
                open={exportDialogOpen}
                onClose={() => setExportDialogOpen(false)}
                exportList={exportingList}
                onExport={exportXML}
                onExclude={excludeFromExport}
                title="Export Selected Cases"
                loading={isValidating}
            />
        </Box>
    );
};